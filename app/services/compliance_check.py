"""Compliance-Check (Leistung „Black Box“): Prüft eine Website auf typische Pflichtangaben und Datenschutz-Grundlagen.

Geprüft werden nur öffentlich abrufbare Seiten (Startseite plus die von dort verlinkten Rechtstexte, max. 5 Abrufe).
Das ist eine automatische, technische Momentaufnahme — KEINE Rechtsberatung. Statische Prüfung: Ob Skripte tatsächlich
erst nach Einwilligung laden, lässt sich ohne Browser nicht abschließend beurteilen (wird als Hinweis ausgewiesen).
Rechtlicher Rahmen (Stand 2026): DDG § 5 (Impressum), DSGVO Art. 13, TDDDG § 25 (Cookies/Endgerätezugriff), BGB/EGBGB
(Widerruf, Verbraucherinformationen), BFSG (Barrierefreiheit), KI-VO Art. 50 (Kennzeichnung von KI-Chatbots).
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from . import seo_check
from .seo_check import SeoCheckError, _f, _get, normalize_url

log = logging.getLogger(__name__)

# Anbieter, deren Einbindung in der Regel eine Einwilligung (TDDDG § 25 / DSGVO) oder mindestens Nennung im Datenschutztext braucht
TRACKERS = [
    ("Google Analytics", r"google-analytics\.com|gtag/js|ga\('create'|analytics\.js", ["google analytics", "analytics"], True),
    ("Google Tag Manager", r"googletagmanager\.com", ["tag manager"], True),
    ("Meta/Facebook Pixel", r"connect\.facebook\.net|fbevents\.js|facebook\.com/tr", ["facebook", "meta"], True),
    ("Hotjar", r"hotjar\.com|static\.hotjar", ["hotjar"], True),
    ("Microsoft Clarity", r"clarity\.ms", ["clarity"], True),
    ("LinkedIn Insight", r"snap\.licdn\.com|px\.ads\.linkedin", ["linkedin"], True),
    ("TikTok Pixel", r"analytics\.tiktok\.com", ["tiktok"], True),
    ("Google Ads", r"googleadservices\.com|doubleclick\.net", ["google ads", "doubleclick", "google"], True),
    ("Google Fonts (extern)", r"fonts\.googleapis\.com|fonts\.gstatic\.com", ["google fonts", "schriftart"], False),
    ("Google Maps", r"maps\.googleapis\.com|google\.com/maps", ["google maps", "maps"], True),
    ("YouTube-Einbettung", r"youtube\.com/embed|youtu\.be/|youtube\.com/iframe_api", ["youtube"], True),
    ("Google reCAPTCHA", r"recaptcha", ["recaptcha"], False),
    ("Externes CDN (IP-Übermittlung)", r"cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com|code\.jquery\.com", ["cdn"], False),
]
CMP = re.compile(r"cookiebot|usercentrics|onetrust|borlabs|complianz|cookieyes|klaro|iubenda|consentmanager|"
                 r"cookie-?consent|cookie-?notice|cookie-?banner|cmp\.js|didomi|trustarc|osano|termly", re.I)
CONSENT_TEXT = re.compile(r"alle akzeptieren|cookies akzeptieren|cookie-einstellungen|cookie einstellungen|"
                          r"nur notwendige|ablehnen|accept all|reject all|manage cookies", re.I)
CHATBOTS = re.compile(r"intercom|drift\.com|tidio|crisp\.chat|zendesk|botpress|chatbase|landbot|freshchat|tawk\.to|"
                      r"hubspot.*conversations|livechat", re.I)
SHOP = re.compile(r"warenkorb|in den warenkorb|add to cart|checkout|kasse|woocommerce|shopify|shopware|jetzt kaufen|"
                  r"bestellen|zur kasse", re.I)
LINK_KINDS = {
    "impressum": re.compile(r"impressum|imprint|legal[- ]notice|anbieterkennzeichnung", re.I),
    "datenschutz": re.compile(r"datenschutz|privacy|data[- ]protection", re.I),
    "agb": re.compile(r"\bagb\b|allgemeine gesch(ä|ae)ftsbedingungen|gesch(ä|ae)ftsbedingungen|terms", re.I),
    "widerruf": re.compile(r"widerruf|withdrawal|r(ü|ue)ckgabe", re.I),
    "barrierefrei": re.compile(r"barrierefreiheit|accessibility", re.I),
}


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []  # (href, text)
        self._a: list | None = None
        self.srcs: list[str] = []
        self.forms: list[dict] = []
        self._form: dict | None = None
        self.text_parts: list[str] = []
        self._skip = 0
        self.headings: list[str] = []
        self._h = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._a = [a["href"], ""]
        elif tag in ("script", "iframe", "img", "link", "source") and (a.get("src") or a.get("href")):
            self.srcs.append(a.get("src") or a.get("href"))
        if tag == "script":
            self._skip += 1
        elif tag in ("style", "noscript"):
            self._skip += 1
        elif tag == "form":
            self._form = {"types": [], "names": []}
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form["types"].append(a.get("type", "text").lower())
            self._form["names"].append(a.get("name", "").lower())
        elif tag in ("h1", "h2", "h3", "h4"):
            self._h = True
            self.headings.append("")

    def handle_endtag(self, tag):
        if tag == "a" and self._a:
            self.anchors.append((self._a[0], " ".join(self._a[1].split())))
            self._a = None
        elif tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == "form":
            self._form = None
        elif tag in ("h1", "h2", "h3", "h4"):
            self._h = False

    def handle_data(self, data):
        if self._a is not None:
            self._a[1] += data
        if self._h and self.headings:
            self.headings[-1] += data
        if not self._skip:
            self.text_parts.append(data)


def _scan(html: str) -> _Scan:
    sc = _Scan()
    try:
        sc.feed(html)
    except Exception:  # pragma: no cover
        log.info("HTML-Parser: unvollständig")
    return sc


def _find_link(sc: _Scan, base: str, kind: str) -> str | None:
    pat = LINK_KINDS[kind]
    best = None
    for href, text in sc.anchors:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if pat.search(text) or pat.search(href):
            url = urljoin(base, href)
            if urlparse(url).scheme in ("http", "https"):
                if pat.search(text):  # Linktext schlägt reinen URL-Treffer
                    return url
                best = best or url
    return best


def _fetch_text(url: str) -> tuple[str, int]:
    resp, body, _, _ = _get(url, max_bytes=800_000)
    if resp.status_code >= 400:
        raise SeoCheckError(f"HTTP {resp.status_code}")
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    sc = _scan(html)
    text = " ".join(" ".join(sc.text_parts).split())
    return text, len(re.findall(r"\w+", text))


def analyze(url: str) -> dict:
    url = normalize_url(url)
    resp, body, elapsed, final_url = _get(url)
    if resp.status_code >= 400:
        raise SeoCheckError(f"Die Seite antwortet mit dem Fehlercode {resp.status_code}.")
    fp = urlparse(final_url)
    base = f"{fp.scheme}://{fp.netloc}/"
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    sc = _scan(html)
    page_text = " ".join(" ".join(sc.text_parts).split())
    blob = html + " " + " ".join(sc.srcs)
    P, C, S, H = "Pflichtangaben", "Cookies & Tracking", "Sicherheit (Grundschutz)", "Weitere Hinweise"
    F: list[dict] = []

    # ---------------- JavaScript-App: Rechtstexte stehen dann nur im Skript-Bundle
    js_links: dict[str, str] = {}
    bundles = seo_check.spa_shell(html, len(re.findall(r"\w+", page_text)))
    if bundles:
        code = ""
        for src in bundles:
            try:
                _r, b, _e, _u = _get(urljoin(base, src), max_bytes=3_000_000)
                code += b.decode("utf-8", errors="replace")
            except SeoCheckError:
                continue
        for kind in ("impressum", "datenschutz"):
            m = re.search(r"[\"'`](/[\w\-/]*(?:" + LINK_KINDS[kind].pattern + r")[\w\-/]*)[\"'`]", code, re.I)
            if m:
                js_links[kind] = urljoin(base, m.group(1))

    # ---------------- Impressum
    imp_url = _find_link(sc, base, "impressum")
    imp_text = ""
    if not imp_url and js_links.get("impressum"):
        F.append(_f("imp", P, "warn", "Impressum (in der JavaScript-App verlinkt)",
                    f"Im Skript der Seite ist ein Impressum unter {js_links['impressum'][:120]} verlinkt. Der Inhalt "
                    "entsteht erst im Browser und konnte nicht automatisch geprüft werden.", 15,
                    "Impressum manuell prüfen und als statische Seite ausliefern (Prerendering), damit Suchmaschinen "
                    "und Prüfwerkzeuge es ohne JavaScript sehen."))
    elif not imp_url:
        F.append(_f("imp", P, "fail", "Impressum verlinkt", "Auf der Startseite wurde kein Link „Impressum“ gefunden.", 15,
                    "Ein Impressum anlegen und von jeder Seite (z. B. im Footer) mit einem Klick erreichbar machen (§ 5 DDG)."))
    else:
        try:
            imp_text, imp_words = _fetch_text(imp_url)
            F.append(_f("imp", P, "ok" if imp_words >= 40 else "warn", "Impressum vorhanden",
                        f"Erreichbar unter {imp_url[:120]} ({imp_words} Wörter).", 15,
                        "Impressum mit allen Pflichtangaben ausbauen."))
        except SeoCheckError:
            F.append(_f("imp", P, "fail", "Impressum erreichbar", f"Der Link {imp_url[:120]} lässt sich nicht abrufen.", 15,
                        "Den Link reparieren, damit das Impressum erreichbar ist."))
    if imp_text:
        plz = re.search(r"\b\d{5}\s+[A-ZÄÖÜ][\wäöüß.\-]+", imp_text)
        street = re.search(r"[A-ZÄÖÜ][\wäöüß.\-]+(?:str\.|straße|strasse|weg|platz|allee|gasse|ring|damm|ufer)\s*\d+", imp_text)
        F.append(_f("imp_addr", P, "ok" if plz and street else "warn" if plz or street else "fail",
                    "Impressum: ladungsfähige Anschrift", "Straße, Hausnummer, PLZ und Ort erkannt." if plz and street
                    else "Anschrift nur teilweise oder gar nicht erkannt.", 6,
                    "Vollständige Anschrift (kein Postfach allein) mit Straße, Hausnummer, PLZ und Ort angeben."))
        mail = re.search(r"[\w.+-]+\s?(@|\(at\)|\[at\])\s?[\w-]+\.[\w.]+", imp_text, re.I)
        F.append(_f("imp_mail", P, "ok" if mail else "fail", "Impressum: E-Mail-Adresse",
                    "E-Mail-Adresse erkannt." if mail else "Keine E-Mail-Adresse erkannt.", 6,
                    "Eine E-Mail-Adresse für schnelle elektronische Kontaktaufnahme angeben."))
        phone = re.search(r"(tel|telefon|fon|phone)[^\n]{0,20}[\d+(]", imp_text, re.I)
        F.append(_f("imp_phone", P, "ok" if phone else "info", "Impressum: Telefonnummer",
                    "Telefonnummer erkannt." if phone else "Keine Telefonnummer erkannt (nicht zwingend, wenn eine "
                    "zweite schnelle Kontaktmöglichkeit besteht).", 2, "Telefonnummer oder Kontaktformular ergänzen."))
        corp = re.search(r"\b(GmbH|UG|AG|e\.\s?V\.|KG|OHG|GbR|SE|Ltd)\b", imp_text)
        if corp:
            rep = re.search(r"Gesch(ä|ae)ftsf(ü|ue)hrer|vertreten durch|Vorstand|vertretungsberechtigt|Inhaber", imp_text, re.I)
            F.append(_f("imp_rep", P, "ok" if rep else "fail", "Impressum: Vertretungsberechtigte",
                        "Vertretung genannt." if rep else "Rechtsform erkannt, aber keine vertretungsberechtigte Person.", 4,
                        "Geschäftsführung/Vorstand bzw. vertretungsberechtigte Person mit Namen nennen."))
            reg = re.search(r"Handelsregister|Registergericht|HR[AB]\s?\d+|Vereinsregister", imp_text, re.I)
            F.append(_f("imp_reg", P, "ok" if reg else "fail", "Impressum: Registerangaben",
                        "Registergericht/Nummer erkannt." if reg else "Bei dieser Rechtsform fehlen Register und Registernummer.", 3,
                        "Registergericht und Registernummer (z. B. HRB 12345, Amtsgericht Frankfurt) ergänzen."))
        vat = re.search(r"DE\s?\d{9}|USt-?Id|Umsatzsteuer-Identifikationsnummer|Umsatzsteuer.?ID", imp_text, re.I)
        F.append(_f("imp_vat", P, "ok" if vat else "info", "Impressum: Umsatzsteuer-ID",
                    "USt-IdNr. erkannt." if vat else "Keine USt-IdNr. erkannt (nur nötig, falls vorhanden).", 2,
                    "Falls vorhanden, die Umsatzsteuer-Identifikationsnummer angeben."))

    # ---------------- Datenschutz
    ds_url = _find_link(sc, base, "datenschutz")
    ds_text = ""
    if not ds_url and js_links.get("datenschutz"):
        F.append(_f("ds", P, "warn", "Datenschutzerklärung (in der JavaScript-App verlinkt)",
                    f"Im Skript der Seite ist eine Datenschutzerklärung unter {js_links['datenschutz'][:120]} verlinkt. "
                    "Der Inhalt entsteht erst im Browser und konnte nicht automatisch geprüft werden.", 15,
                    "Datenschutzerklärung manuell prüfen und als statische Seite ausliefern (Prerendering)."))
    elif not ds_url:
        F.append(_f("ds", P, "fail", "Datenschutzerklärung verlinkt", "Auf der Startseite wurde kein Link zur Datenschutzerklärung gefunden.", 15,
                    "Eine Datenschutzerklärung nach Art. 13 DSGVO bereitstellen und von jeder Seite verlinken."))
    else:
        try:
            ds_text, ds_words = _fetch_text(ds_url)
            F.append(_f("ds", P, "ok" if ds_words >= 500 else "warn" if ds_words >= 250 else "fail", "Datenschutzerklärung vorhanden",
                        f"Erreichbar unter {ds_url[:120]} ({ds_words} Wörter).", 15,
                        "Die Erklärung ist sehr kurz; für die eingesetzten Dienste ist meist deutlich mehr Text nötig."))
        except SeoCheckError:
            F.append(_f("ds", P, "fail", "Datenschutzerklärung erreichbar", f"Der Link {ds_url[:120]} lässt sich nicht abrufen.", 15,
                        "Den Link reparieren."))
    if ds_text:
        low = ds_text.lower()
        groups = [
            ("Verantwortliche Stelle", r"verantwortliche|verantwortlicher|verantwortlich im sinne", "Verantwortliche Stelle mit Kontaktdaten nennen."),
            ("Betroffenenrechte", r"auskunft.*l(ö|oe)schung|l(ö|oe)schung.*auskunft|widerspruch", "Rechte auf Auskunft, Berichtigung, Löschung, Widerspruch und Datenübertragbarkeit aufführen."),
            ("Beschwerderecht Aufsichtsbehörde", r"aufsichtsbeh(ö|oe)rde|beschwerde", "Beschwerderecht bei einer Aufsichtsbehörde nennen."),
            ("Rechtsgrundlagen", r"art\.?\s?6|berechtigtes interesse|einwilligung|dsgvo", "Zu jedem Zweck die Rechtsgrundlage (Art. 6 DSGVO) nennen."),
            ("Speicherdauer", r"speicherdauer|gel(ö|oe)scht|aufbewahr|speicherfrist", "Angaben zur Speicherdauer bzw. zu den Löschfristen ergänzen."),
            ("Hosting und Server-Logfiles", r"server-?log|hosting|logfile|ip-adresse|hoster", "Hosting-Anbieter und Server-Logfiles beschreiben."),
            ("Empfänger und Drittlandtransfer", r"auftragsverarbeit|empf(ä|ae)nger|drittland|standardvertragsklauseln|usa", "Empfänger/Auftragsverarbeiter und ggf. Drittlandübermittlungen nennen."),
            ("Cookies und Tracking", r"cookie|tracking|analyse", "Einsatz von Cookies und Tracking-Diensten erläutern."),
        ]
        for i, (label, pat, fix) in enumerate(groups):
            hit = bool(re.search(pat, low, re.S))
            F.append(_f(f"ds{i}", P, "ok" if hit else "warn", f"Datenschutz: {label}",
                        "Im Text erkennbar." if hit else "Im Text nicht erkennbar.", 3, fix))

    # ---------------- Shop-spezifisch
    is_shop = len(SHOP.findall(page_text + " " + blob)) >= 2
    if is_shop:
        for kind, label, w, fix in [
            ("agb", "Allgemeine Geschäftsbedingungen (AGB)", 6, "AGB bereitstellen und im Bestellprozess einbeziehen."),
            ("widerruf", "Widerrufsbelehrung", 8, "Widerrufsbelehrung und Muster-Widerrufsformular für Verbraucher bereitstellen."),
        ]:
            u = _find_link(sc, base, kind)
            F.append(_f(kind, P, "ok" if u else "fail", label, f"Link gefunden: {u[:110]}" if u else "Kein Link gefunden.", w, fix))
        u = _find_link(sc, base, "barrierefrei")
        F.append(_f("bfsg", P, "ok" if u else "warn", "Erklärung zur Barrierefreiheit (BFSG)",
                    "Link gefunden." if u else "Kein Link gefunden. Für Online-Shops mit Verbraucherkunden gilt seit 28.06.2025 das BFSG "
                    "(Ausnahmen für Kleinstunternehmen).", 3, "Prüfen, ob das BFSG gilt, und ggf. eine Barrierefreiheitserklärung veröffentlichen."))
    else:
        F.append(_f("shop", H, "info", "Online-Shop", "Kein Online-Shop erkannt — AGB, Widerrufsbelehrung und Preisangaben wurden daher nicht verlangt.", 0))

    # ---------------- Cookies & Tracking
    cookies_hdr = []
    try:
        cookies_hdr = resp.raw.headers.getlist("Set-Cookie")
    except Exception:
        h = resp.headers.get("Set-Cookie")
        cookies_hdr = [h] if h else []
    cmp = bool(CMP.search(blob)) or bool(CONSENT_TEXT.search(page_text))
    found = [(name, needs, vend) for name, pat, vend, needs in TRACKERS if re.search(pat, blob, re.I)]
    consent_needed = [n for n, needs, _ in found if needs]
    if consent_needed:
        F.append(_f("cmp", C, "ok" if cmp else "fail", "Einwilligungsbanner (Cookie-Consent)",
                    f"Consent-Lösung erkannt. Eingebunden sind u. a.: {', '.join(consent_needed)}." if cmp
                    else f"Tracking-/Einbettungsdienste gefunden ({', '.join(consent_needed)}), aber kein Einwilligungsbanner erkannt.", 12,
                    "Einwilligungsbanner (Consent-Manager) einsetzen und diese Dienste erst nach aktiver Einwilligung laden (§ 25 TDDDG)."))
        if cmp:
            F.append(_f("cmp_static", C, "info", "Laden nach Einwilligung",
                        "Ob die Dienste tatsächlich erst nach Zustimmung laden, kann diese statische Prüfung nicht beurteilen. "
                        "Bitte im Browser mit geleertem Cache und ohne Zustimmung testen (Netzwerk-Tab).", 0))
    else:
        F.append(_f("cmp", C, "ok", "Keine einwilligungspflichtigen Dienste erkannt",
                    "Im HTML wurden keine bekannten Tracking- oder Einbettungsdienste gefunden." + (" Ein Cookie-Hinweis ist vorhanden." if cmp else ""), 12))
    bad_names = [c.split("=")[0].strip() for c in cookies_hdr if re.match(r"\s*(_ga|_gid|_gcl|_fbp|_hj|_clck|__utm|_pk_)", c)]
    F.append(_f("cookies_set", C, "fail" if bad_names else "ok", "Cookies beim ersten Aufruf",
                f"Tracking-Cookies schon ohne Einwilligung gesetzt: {', '.join(bad_names)}." if bad_names
                else (f"Gesetzte Cookies: {', '.join(c.split('=')[0].strip() for c in cookies_hdr)[:200]}." if cookies_hdr
                      else "Beim ersten Aufruf werden keine Cookies gesetzt."), 8,
                "Tracking-Cookies erst nach Einwilligung setzen."))
    ext_fonts = [n for n, needs, _ in found if n.startswith("Google Fonts")]
    F.append(_f("fonts", C, "fail" if ext_fonts else "ok", "Schriftarten lokal eingebunden",
                "Schriftarten werden von Google-Servern geladen (IP-Übermittlung; Rechtsprechung mahnt hier wiederholt ab)." if ext_fonts
                else "Keine externen Google-Fonts erkannt.", 4, "Schriftarten lokal auf dem eigenen Server hosten."))
    if ds_text:
        low = ds_text.lower()
        missing = [n for n, needs, vend in found if not any(v in low for v in vend)]
        F.append(_f("ds_tools", C, "fail" if missing else "ok", "Eingesetzte Dienste in der Datenschutzerklärung",
                    ("Nicht in der Erklärung erwähnt: " + ", ".join(missing)) if missing
                    else "Alle erkannten Dienste werden in der Datenschutzerklärung erwähnt.", 6 if found else 0,
                    "Jeden eingesetzten Dienst mit Zweck, Rechtsgrundlage und Empfänger in der Datenschutzerklärung beschreiben."))

    # ---------------- Sicherheit
    hs = {k.lower(): v for k, v in resp.headers.items()}
    https = fp.scheme == "https"
    F.append(_f("https", S, "ok" if https else "fail", "HTTPS", "Verschlüsselte Verbindung." if https else "Seite ohne HTTPS.", 8,
                "HTTPS einrichten und erzwingen (Art. 32 DSGVO)."))
    for key, label, w, fix in [
        ("strict-transport-security", "HSTS (erzwingt HTTPS)", 3, "Header Strict-Transport-Security setzen."),
        ("x-content-type-options", "X-Content-Type-Options", 1, "Header X-Content-Type-Options: nosniff setzen."),
        ("referrer-policy", "Referrer-Policy", 1, "Referrer-Policy setzen, z. B. strict-origin-when-cross-origin."),
        ("content-security-policy", "Content-Security-Policy", 2, "Eine Content-Security-Policy gegen Einschleusen fremder Skripte einführen."),
    ]:
        F.append(_f(key, S, "ok" if key in hs else "warn", f"Sicherheits-Header: {label}",
                    "Gesetzt." if key in hs else "Nicht gesetzt.", w, fix))

    # ---------------- Weitere Hinweise
    if CHATBOTS.search(blob):
        F.append(_f("chatbot", H, "info", "Chatbot/Live-Chat erkannt",
                    "Wenn der Chat KI nutzt, muss den Nutzer:innen erkennbar sein, dass sie mit einer KI sprechen (Art. 50 KI-VO).", 0))
    for form in sc.forms:
        if "email" in form["types"] or any("mail" in n for n in form["names"]):
            near = bool(re.search(r"datenschutz|privacy", page_text, re.I))
            newsletter = bool(re.search(r"newsletter", page_text, re.I))
            F.append(_f("form", H, "ok" if near else "warn", "Formular mit E-Mail-Feld",
                        "Datenschutzhinweis auf der Seite vorhanden." if near else "Formular ohne erkennbaren Datenschutzhinweis.", 3,
                        "Bei jedem Formular auf die Datenschutzerklärung hinweisen (Art. 13 DSGVO)."))
            if newsletter:
                F.append(_f("nl", H, "info", "Newsletter-Anmeldung", "Für Newsletter ist ein Double-Opt-in mit Protokoll üblich und rechtlich geboten.", 0))
            break

    scored = [f for f in F if f["status"] != "info" and f["weight"] > 0]
    weight = sum(f["weight"] for f in scored)
    got = sum(f["weight"] * (1 if f["status"] == "ok" else 0.5 if f["status"] == "warn" else 0) for f in scored)
    return {"url": final_url, "score": round(100 * got / weight) if weight else 0, "findings": F,
            "facts": {"impressum": imp_url or js_links.get("impressum", ""),
                      "datenschutz": ds_url or js_links.get("datenschutz", ""), "js_app": bool(bundles), "shop": is_shop,
                      "consent_manager": cmp, "dienste": [n for n, _, _ in found], "https": https}}


_SYSTEM = ("Du bist ein Berater für Website-Compliance bei {CLUB} und schreibst einen Prüfbericht für Unternehmer:innen "
           "ohne juristisches Vorwissen. Ton: ruhig, klar, sachlich, Deutsch, du-Form. WICHTIG: Du gibst keine Rechtsberatung. "
           "Formuliere Ergebnisse als Hinweise und Empfehlungen, nenne keine Bußgeld- oder Abmahnsummen und erkläre, dass "
           "die Prüfung automatisch und statisch ist und bei Unsicherheit eine Anwältin, ein Anwalt oder ein Datenschutzbeauftragter "
           "entscheiden sollte. Nutze NUR die Prüfergebnisse (erfinde nichts). Sortiere die Maßnahmen nach Dringlichkeit "
           "(kritisch zuerst), dann nach Wirkung geteilt durch Aufwand. Antworte NUR als JSON: {\"zusammenfassung\": \"3–4 Sätze\", "
           "\"massnahmen\": [{\"prio\": 1, \"titel\": \"…\", \"warum\": \"1–2 Sätze, warum das relevant ist\", \"so_gehts\": "
           "\"konkrete Schritte, 1–3 Sätze\", \"aufwand\": \"gering|mittel|hoch\", \"wirkung\": \"hoch|mittel|niedrig\"}] "
           "(4–7 Einträge, nur echte Schwächen; bei kaum Schwächen weniger), \"keyword_hinweise\": \"\", "
           "\"naechste_schritte\": \"2 Sätze, inkl. Empfehlung zur rechtlichen Prüfung\"}. "
           "Rechtliche Bezüge: Nenne ausschließlich diese Vorschriften und nur, wo sie zum Prüfpunkt passen: § 5 DDG (Impressum; das "
           "frühere TMG gilt nicht mehr), Art. 13 und Art. 32 DSGVO, § 25 TDDDG (Cookies/Einwilligung), §§ 312 ff. BGB (Widerruf bei "
           "Verbraucherverträgen), BFSG (Barrierefreiheit) und Art. 50 KI-VO (nur bei KI-Chatbots). Externe Schriftarten "
           "(Google Fonts) und externe CDNs: Art. 6 Abs. 1 DSGVO – Übermittlung der IP-Adresse ohne Rechtsgrundlage (LG München I, "
           "Urteil vom 20.01.2022, 3 O 17493/20); dafür NICHT § 25 TDDDG nennen. Keine anderen Gesetze, keine "
           "Paragrafen erfinden, keine allgemeinen Aussagen über Behörden oder die KI-Verordnung bei Sicherheits-Headern. "
           "Ist erkannt.js_app true, sind Rechtstexte in einer JavaScript-App verlinkt: nicht als fehlend darstellen, sondern "
           "manuelle Prüfung und statische Auslieferung empfehlen. Ein fehlender Consent-Manager ist nur dann eine Lücke, "
           "wenn einwilligungspflichtige Dienste erkannt wurden (erkannt.dienste); ohne solche Dienste ist kein Cookie-Banner "
           "nötig. Durchgehend du-Form. "
           "WICHTIG für gültiges JSON: In Textwerten keine doppelten Anführungszeichen (nutze ‚einfache‘), keine Zeilenumbrüche.")


def build_report(result: dict) -> dict:
    payload = {"url": result["url"], "score": result["score"], "erkannt": result["facts"],
               "ergebnisse": [{k: f[k] for k in ("cat", "status", "title", "detail", "fix")} for f in result["findings"]
                              if f["status"] != "ok" or f["cat"] == "Pflichtangaben"]}
    rep = seo_check.ai_report(_SYSTEM, payload, result)
    rep["keyword_hinweise"] = ""  # gibt es beim Compliance-Check nicht
    if not rep["ai"]:
        rep["naechste_schritte"] = ("Setze die Maßnahmen der Reihe nach um und lass Impressum und Datenschutzerklärung "
                                    "zusätzlich rechtlich prüfen. Danach kannst du die Seite erneut prüfen lassen.")
    return rep
