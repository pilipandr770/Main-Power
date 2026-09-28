"""SEO-Check (Leistung „Black Box“): Öffentliche Seite abrufen, technisch/inhaltlich prüfen, Aiko schreibt den Bericht.

Es wird nur die eine öffentliche Seite plus robots.txt und sitemap.xml abgerufen (kein Crawl, keine Anmeldung).
Schutz vor SSRF: nur http(s), nur öffentliche IP-Adressen (auch nach Weiterleitungen), Größen- und Zeitlimit.
Der Bericht ist eine Momentaufnahme und keine Ranking-Garantie.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from .llm import LLMUnavailable, complete

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; MainPowerSEOCheck/1.0; +https://mainpower.andrii-it.de)"
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 8


class SeoCheckError(Exception):
    """Nutzerverständliche Fehlermeldung (Deutsch)."""


# --------------------------------------------------------------------------- Abruf mit SSRF-Schutz
def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise SeoCheckError("Bitte gib die Adresse deiner Website ein.")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname or " " in raw:
        raise SeoCheckError("Das ist keine gültige Web-Adresse (z. B. https://www.beispiel.de).")
    if p.username or p.password:
        raise SeoCheckError("Adressen mit Zugangsdaten sind nicht erlaubt.")
    if p.port not in (None, 80, 443):
        raise SeoCheckError("Es sind nur die Standard-Ports 80 und 443 erlaubt.")
    return raw[:500]


def _assert_public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise SeoCheckError("Diese Domain konnte nicht aufgelöst werden. Ist die Adresse richtig geschrieben?")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise SeoCheckError("Diese Adresse ist nicht öffentlich erreichbar und kann nicht geprüft werden.")


def _get(url: str, max_bytes: int = MAX_BYTES) -> tuple[requests.Response, bytes, float, str]:
    """GET mit manuell verfolgten Weiterleitungen (max. 4), jede Station wird auf öffentliche IPs geprüft."""
    hops = 0
    while True:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise SeoCheckError("Die Seite leitet auf eine ungültige Adresse weiter.")
        _assert_public(p.hostname)
        t0 = time.monotonic()
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"}, timeout=TIMEOUT,
                             allow_redirects=False, stream=True)
        except requests.exceptions.SSLError:
            raise SeoCheckError("Das HTTPS-Zertifikat der Seite ist ungültig oder abgelaufen — das ist schon ein wichtiger SEO-Befund.")
        except requests.RequestException:
            raise SeoCheckError("Die Seite ist nicht erreichbar (Zeitüberschreitung oder Verbindungsfehler).")
        if r.is_redirect and r.headers.get("Location"):
            hops += 1
            if hops > 4:
                raise SeoCheckError("Zu viele Weiterleitungen hintereinander.")
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > max_bytes:
                break
        elapsed = time.monotonic() - t0
        r.close()
        return r, body[:max_bytes], elapsed, url


# --------------------------------------------------------------------------- HTML-Auswertung
class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []  # (rel, href) für <link>
        self.h: dict[str, list[str]] = {f"h{i}": [] for i in range(1, 7)}
        self._h_open: str | None = None
        self.imgs = 0
        self.imgs_alt = 0
        self.anchors: list[str] = []
        self.jsonld = 0
        self._script_ld = False
        self.lang = ""
        self.text_parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            self.links.append((a.get("rel", "").lower(), a.get("href", "")))
        elif tag in self.h:
            self._h_open = tag
            self.h[tag].append("")
        elif tag == "img":
            self.imgs += 1
            if a.get("alt", "").strip():
                self.imgs_alt += 1
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"])
        elif tag == "script":
            self._skip += 1
            self._script_ld = "ld+json" in a.get("type", "").lower()
            if self._script_ld:
                self.jsonld += 1
        elif tag in ("style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.h:
            self._h_open = None
        elif tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
            self._script_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._h_open and self.h[self._h_open]:
            self.h[self._h_open][-1] += data
        if not self._skip:
            self.text_parts.append(data)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


# --------------------------------------------------------------------------- Prüfungen
def _f(key, cat, status, title, detail, weight, fix=""):
    return {"key": key, "cat": cat, "status": status, "title": title, "detail": detail, "weight": weight, "fix": fix}


def analyze(url: str, keywords: list[str]) -> dict:
    url = normalize_url(url)
    resp, body, elapsed, final_url = _get(url)
    fp = urlparse(final_url)
    base = f"{fp.scheme}://{fp.netloc}"
    findings: list[dict] = []

    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code >= 400:
        raise SeoCheckError(f"Die Seite antwortet mit dem Fehlercode {resp.status_code}.")
    if "html" not in ctype.lower() and body[:200].lstrip()[:1] != b"<":
        raise SeoCheckError("Unter dieser Adresse liegt keine Webseite (kein HTML).")
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    pg = _Page()
    try:
        pg.feed(html)
    except Exception:  # pragma: no cover - kaputtes HTML
        log.info("HTML-Parser: unvollständig")
    text = " ".join(pg.text_parts)
    words = _words(text)
    title = " ".join(pg.title.split())
    desc = pg.meta.get("description", "").strip()
    h1s = [" ".join(t.split()) for t in pg.h["h1"] if t.strip()]
    T, I, S, K = "Technik", "Inhalt", "Snippets & Social", "Keywords"

    # Technik
    https_ok = fp.scheme == "https"
    findings.append(_f("https", T, "ok" if https_ok else "fail", "Sichere Verbindung (HTTPS)",
                       "Die Seite wird über HTTPS ausgeliefert." if https_ok else "Die Seite lädt ohne HTTPS.", 10,
                       "HTTPS einrichten und alle http-Adressen dauerhaft (301) auf https umleiten."))
    st = "ok" if elapsed < 1.0 else "warn" if elapsed < 2.5 else "fail"
    findings.append(_f("speed", T, st, "Antwortzeit des Servers", f"{elapsed:.2f} Sekunden bis das HTML da ist.", 6,
                       "Caching aktivieren, Hosting/Plugins prüfen, Bilder und Skripte verkleinern."))
    kb = len(body) / 1024
    findings.append(_f("size", T, "ok" if kb < 500 else "warn", "Größe der HTML-Seite", f"{kb:.0f} KB.", 3,
                       "Unnötigen Code und eingebettete Daten aus dem HTML entfernen."))
    robots_url = f"{base}/robots.txt"
    robots_txt, sitemap_in_robots = "", False
    try:
        rr, rb, _, _ = _get(robots_url, max_bytes=100_000)
        if rr.status_code == 200 and rb:
            robots_txt = rb.decode("utf-8", errors="replace")
            sitemap_in_robots = bool(re.search(r"(?im)^\s*sitemap:", robots_txt))
    except SeoCheckError:
        pass
    blocks_all = bool(re.search(r"(?is)user-agent:\s*\*\s*(?:[^\n]*\n)*?\s*disallow:\s*/\s*$", robots_txt, re.M))
    if not robots_txt:
        findings.append(_f("robots", T, "warn", "robots.txt", "Keine robots.txt gefunden.", 5,
                           "Eine einfache robots.txt anlegen und darin die Sitemap nennen."))
    elif blocks_all:
        findings.append(_f("robots", T, "fail", "robots.txt sperrt die ganze Seite",
                           "Die Datei enthält „Disallow: /“ für alle Suchmaschinen.", 12,
                           "Die Sperre entfernen, sonst wird die Seite nicht in Suchergebnissen erscheinen."))
    else:
        findings.append(_f("robots", T, "ok", "robots.txt", "Vorhanden und sperrt die Seite nicht.", 5))
    sm_ok = sitemap_in_robots
    if not sm_ok:
        try:
            sr, sb, _, _ = _get(f"{base}/sitemap.xml", max_bytes=200_000)
            sm_ok = sr.status_code == 200 and (b"<urlset" in sb[:5000] or b"<sitemapindex" in sb[:5000])
        except SeoCheckError:
            sm_ok = False
    findings.append(_f("sitemap", T, "ok" if sm_ok else "warn", "XML-Sitemap",
                       "Sitemap gefunden." if sm_ok else "Keine Sitemap gefunden.", 5,
                       "Eine sitemap.xml erzeugen (die meisten CMS können das) und in der robots.txt eintragen."))
    canon = next((h for rel, h in pg.links if "canonical" in rel), "")
    findings.append(_f("canonical", T, "ok" if canon else "warn", "Canonical-Link",
                       f"Gesetzt auf {canon[:120]}" if canon else "Kein Canonical-Link.", 4,
                       "Pro Seite ein <link rel=\"canonical\"> auf die bevorzugte Adresse setzen (vermeidet Duplikate)."))
    robots_meta = pg.meta.get("robots", "").lower()
    noindex = "noindex" in robots_meta
    findings.append(_f("noindex", T, "fail" if noindex else "ok", "Indexierung erlaubt",
                       "Die Seite trägt „noindex“ und wird nicht in Suchergebnissen gelistet." if noindex
                       else "Kein „noindex“ gefunden.", 12,
                       "Das noindex-Tag entfernen, wenn die Seite gefunden werden soll."))
    findings.append(_f("lang", T, "ok" if pg.lang else "warn", "Sprachangabe", f"lang=\"{pg.lang}\"" if pg.lang else "Keine Sprache im <html>-Tag.", 2,
                       "Im <html>-Tag lang=\"de\" (oder die passende Sprache) setzen."))
    findings.append(_f("viewport", T, "ok" if "viewport" in pg.meta else "fail", "Mobile Darstellung (Viewport)",
                       "Viewport-Angabe vorhanden." if "viewport" in pg.meta else "Keine Viewport-Angabe — mobil oft unbrauchbar.", 5,
                       "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> ergänzen und Layout mobil prüfen."))

    # Inhalt
    tl = len(title)
    findings.append(_f("title", I, "ok" if 30 <= tl <= 62 else "warn" if tl else "fail", "Seitentitel (Title)",
                       f"„{title[:90]}“ ({tl} Zeichen)" if tl else "Kein Title gefunden.", 10,
                       "Einen eindeutigen Titel mit 30–60 Zeichen schreiben, wichtigstes Thema/Keyword vorn."))
    dl = len(desc)
    findings.append(_f("description", I, "ok" if 70 <= dl <= 165 else "warn" if dl else "fail", "Meta-Beschreibung",
                       f"{dl} Zeichen." if dl else "Keine Meta-Beschreibung.", 8,
                       "Eine Beschreibung mit 70–160 Zeichen schreiben, die zum Klick einlädt."))
    findings.append(_f("h1", I, "ok" if len(h1s) == 1 else "warn" if len(h1s) > 1 else "fail", "Hauptüberschrift (H1)",
                       f"„{h1s[0][:90]}“" if len(h1s) == 1 else (f"{len(h1s)} H1-Überschriften." if h1s else "Keine H1."), 7,
                       "Genau eine H1 pro Seite, die das Thema klar benennt."))
    h2n = len(pg.h["h2"])
    findings.append(_f("h2", I, "ok" if h2n >= 2 else "warn", "Gliederung (H2/H3)", f"{h2n} Zwischenüberschriften (H2).", 3,
                       "Den Text mit mehreren H2-Zwischenüberschriften gliedern."))
    findings.append(_f("words", I, "ok" if words >= 300 else "warn" if words >= 120 else "fail", "Textumfang",
                       f"ca. {words} Wörter.", 5, "Mehr hilfreichen, eigenständigen Text bieten (Richtwert ab 300 Wörter)."))
    if pg.imgs:
        ratio = pg.imgs_alt / pg.imgs
        findings.append(_f("alt", I, "ok" if ratio >= 0.9 else "warn" if ratio >= 0.5 else "fail", "Alt-Texte bei Bildern",
                           f"{pg.imgs_alt} von {pg.imgs} Bildern haben einen Alt-Text.", 5,
                           "Jedem inhaltlichen Bild eine kurze, beschreibende Alternativbeschreibung geben."))
    host = fp.hostname or ""
    internal = [a for a in pg.anchors if a.startswith("/") or host in a]
    findings.append(_f("links", I, "ok" if len(internal) >= 3 else "warn", "Interne Verlinkung",
                       f"{len(internal)} interne Links.", 3, "Wichtige Seiten untereinander verlinken, mit sprechenden Linktexten."))

    # Snippets & Social
    og = [k for k in ("og:title", "og:description", "og:image") if pg.meta.get(k)]
    findings.append(_f("og", S, "ok" if len(og) == 3 else "warn" if og else "fail", "Open Graph (Vorschau in sozialen Netzwerken)",
                       f"{len(og)} von 3 Angaben vorhanden.", 4, "og:title, og:description und og:image setzen — bestimmt die Vorschau bei LinkedIn, WhatsApp & Co."))
    findings.append(_f("jsonld", S, "ok" if pg.jsonld else "warn", "Strukturierte Daten (JSON-LD)",
                       f"{pg.jsonld} Block/Blöcke gefunden." if pg.jsonld else "Keine strukturierten Daten.", 5,
                       "Schema.org-Auszeichnung ergänzen (z. B. Organization, LocalBusiness, Event, Article)."))

    # Keywords
    kws = [k.strip() for k in keywords if k.strip()][:5]
    lower = text.lower()
    for kw in kws:
        k = kw.lower()
        in_t, in_h1, in_d = k in title.lower(), any(k in h.lower() for h in h1s), k in desc.lower()
        n = lower.count(k)
        parts = [("Title", in_t), ("H1", in_h1), ("Beschreibung", in_d)]
        hit = sum(1 for _, v in parts if v)
        findings.append(_f(f"kw:{kw}", K, "ok" if hit >= 2 and n >= 2 else "warn" if hit or n else "fail",
                           f"Keyword „{kw}“",
                           f"In {', '.join(n_ for n_, v in parts if v) or 'keinem der Kernelemente'}; {n}× im Text.", 6,
                           f"„{kw}“ natürlich in Title, H1 und Beschreibung sowie im Text verwenden — ohne Keyword-Stopfen."))

    weight = sum(f["weight"] for f in findings)
    got = sum(f["weight"] * (1 if f["status"] == "ok" else 0.5 if f["status"] == "warn" else 0) for f in findings)
    score = round(100 * got / weight) if weight else 0
    return {"url": final_url, "score": score, "findings": findings,
            "facts": {"title": title, "description": desc, "h1": h1s[:3], "words": words, "seconds": round(elapsed, 2),
                      "kb": round(kb), "lang": pg.lang}}


# --------------------------------------------------------------------------- Bericht
def _fallback_report(result: dict) -> dict:
    bad = [f for f in result["findings"] if f["status"] != "ok"]
    bad.sort(key=lambda f: (f["status"] != "fail", -f["weight"]))
    actions = [{"prio": i + 1, "titel": f["title"], "warum": f["detail"], "so_gehts": f["fix"],
                "aufwand": "mittel", "wirkung": "hoch" if f["weight"] >= 8 else "mittel" if f["weight"] >= 5 else "niedrig"}
               for i, f in enumerate(bad[:6])]
    s = result["score"]
    verdict = "sehr gut aufgestellt" if s >= 85 else "solide, mit klaren Verbesserungsmöglichkeiten" if s >= 65 \
        else "mit deutlichem Nachholbedarf"
    return {"zusammenfassung": f"Ergebnis der Prüfung: {verdict} (Score {s}/100). "
                               f"Die wichtigsten Hebel stehen unten in der Reihenfolge ihrer Wirkung.",
            "massnahmen": actions, "keyword_hinweise": "", "naechste_schritte":
            "Die Maßnahmen der Reihe nach umsetzen und die Seite danach erneut prüfen lassen.", "ai": False}


def build_report(result: dict, keywords: list[str]) -> dict:
    system = ("Du bist ein erfahrener SEO-Berater bei {CLUB} und schreibst einen Prüfbericht für Unternehmer:innen "
              "ohne SEO-Vorwissen. Ton: ruhig, klar, wertschätzend, Deutsch, du-Form. Nutze NUR die Prüfergebnisse "
              "(erfinde keine Zahlen, Rankings oder Wettbewerber). Sortiere die Maßnahmen nach Wirkung geteilt durch "
              "Aufwand. Verspreche niemals bestimmte Platzierungen oder Besucherzahlen. Antworte NUR als JSON: "
              "{\"zusammenfassung\": \"3–4 Sätze\", \"massnahmen\": [{\"prio\": 1, \"titel\": \"…\", \"warum\": \"1–2 "
              "Sätze, warum das zählt\", \"so_gehts\": \"konkrete Schritte, 1–3 Sätze\", \"aufwand\": \"gering|mittel|hoch\", "
              "\"wirkung\": \"hoch|mittel|niedrig\"}] (5–7 Einträge, nur echte Schwächen), \"keyword_hinweise\": \"2–3 "
              "Sätze, leer wenn keine Keywords geprüft\", \"naechste_schritte\": \"2 Sätze\"}. "
              "WICHTIG für gültiges JSON: In Textwerten keine doppelten Anführungszeichen (nutze ‚einfache‘), keine "
              "Zeilenumbrüche, keine Aufzählungszeichen.")
    payload = {"url": result["url"], "score": result["score"], "seite": result["facts"], "keywords": keywords,
               "ergebnisse": [{k: f[k] for k in ("cat", "status", "title", "detail", "fix")} for f in result["findings"]]}
    return ai_report(system, payload, result)


def ai_report(system: str, payload: dict, result: dict) -> dict:
    """Bericht per Modell (JSON) mit zweitem Versuch; ohne KI oder bei Fehlern: regelbasierter Fallback."""
    data = None
    started = time.monotonic()
    for attempt in (1, 2):
        if attempt == 2 and time.monotonic() - started > 30:
            break  # Gesamtlaufzeit begrenzen (Proxy-Limit ca. 100 s)
        try:
            raw = complete(system + " Fasse dich kurz: jeder Textwert höchstens zwei Sätze, insgesamt höchstens 6 Maßnahmen." +
                           (" Deine letzte Antwort war kein gültiges JSON (vermutlich zu lang). Antworte kompakter und achte "
                            "streng auf die Syntax." if attempt == 2 else ""),
                           [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                           max_tokens=3200, purpose="seo_report")
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1], strict=False)
            break
        except LLMUnavailable as exc:
            log.info("SEO-Bericht per Fallback (%s)", exc)
            return _fallback_report(result)
        except ValueError as exc:
            log.warning("SEO-Bericht: ungültiges JSON (Versuch %s): %s", attempt, exc)
    if data is None:
        return _fallback_report(result)
    try:
        acts = [{"prio": int(a.get("prio", i + 1)), "titel": str(a.get("titel", ""))[:160],
                 "warum": str(a.get("warum", ""))[:500], "so_gehts": str(a.get("so_gehts", ""))[:600],
                 "aufwand": str(a.get("aufwand", "mittel")), "wirkung": str(a.get("wirkung", "mittel"))}
                for i, a in enumerate(data.get("massnahmen", [])[:8]) if a.get("titel")]
        if not acts:
            raise ValueError("keine Maßnahmen")
        return {"zusammenfassung": str(data.get("zusammenfassung", ""))[:1200], "massnahmen": acts,
                "keyword_hinweise": str(data.get("keyword_hinweise", ""))[:800],
                "naechste_schritte": str(data.get("naechste_schritte", ""))[:600], "ai": True}
    except (LLMUnavailable, ValueError, KeyError, TypeError) as exc:
        log.info("SEO-Bericht per Fallback (%s)", exc)
        return _fallback_report(result)
