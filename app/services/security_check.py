"""Sicherheits-Check (passiv) — Leistung „Black Box“.

Wie ein normaler Browser- bzw. Mailserver-Besuch: EIN HTTPS-Abruf, EIN TLS-Handshake auf Port 443 und öffentliche
DNS-Abfragen (SPF, DMARC, DKIM-Selektoren, MX, CAA, DNSSEC) sowie /.well-known/security.txt. Kein Portscan, kein Schwachstellen-
scan, keine Angriffsmuster. Das ist ausdrücklich KEIN Penetrationstest. Schutz vor SSRF: nur öffentliche IP-Adressen.
"""
from __future__ import annotations

import logging
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import seo_check
from .seo_check import SeoCheckError, _assert_public, _f

log = logging.getLogger(__name__)

TLS, HDR, COO, MAIL, DNS = "Verschlüsselung (TLS)", "HTTP-Sicherheits-Header", "Cookies", "E-Mail-Sicherheit", "DNS und Kontakt"
# Gängige Selektoren großer Mailanbieter (Google, Microsoft 365, Zoho, Mailchimp, Proton, Fastmail, Hostinger, IONOS …)
DKIM_SELECTORS = ["default", "google", "selector1", "selector2", "zmail", "zoho", "k1", "k2", "s1", "s2", "mail", "dkim",
                  "protonmail", "protonmail2", "fm1", "fm2", "hostingermail1", "hostingermail2", "smtp", "key1", "mx"]
SECOND_LEVEL = {"co.uk", "org.uk", "com.au", "co.nz", "com.br", "co.za"}


def hostname_from(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise SeoCheckError("Bitte gib die Domain deiner Website ein.")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise SeoCheckError("Das ist keine gültige Domain (z. B. beispiel.de).")
    host = (p.hostname or "").lower().rstrip(".")
    if not host or " " in raw or "." not in host or p.username or p.password:
        raise SeoCheckError("Das ist keine gültige Domain (z. B. beispiel.de).")
    if re.fullmatch(r"[\d.:]+", host):
        raise SeoCheckError("Bitte eine Domain statt einer IP-Adresse angeben.")
    if p.port not in (None, 80, 443):
        raise SeoCheckError("Es sind nur die Standard-Ports 80 und 443 erlaubt.")
    return host


def org_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# --------------------------------------------------------------------------- Netzwerk-Bausteine (einzeln testbar)
def _tls(host: str) -> dict:
    """Ein TLS-Handshake auf 443 gegen eine geprüft öffentliche IP."""
    _assert_public(host)
    ip = next((i[4][0] for i in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)), None)
    if not ip:
        return {"reachable": False, "error": "keine Adresse"}
    out: dict = {"reachable": False}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=6) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                cert = s.getpeercert()
                out.update(reachable=True, valid=True, version=s.version(), cipher=(s.cipher() or ("", "", 0))[0])
                out["not_after"] = ssl.cert_time_to_seconds(cert["notAfter"])
                out["issuer"] = " ".join(v for rdn in cert.get("issuer", ()) for k, v in rdn if k == "organizationName")
    except ssl.SSLCertVerificationError as exc:
        out.update(reachable=True, valid=False, error=str(exc.verify_message or exc)[:160])
    except (OSError, ssl.SSLError) as exc:
        out.update(reachable=False, error=str(exc)[:160])
    return out


def _txt(name: str) -> list[str]:
    import dns.exception
    import dns.resolver
    r = dns.resolver.Resolver()
    r.lifetime = 2
    try:
        return ["".join(p.decode() for p in rr.strings) for rr in r.resolve(name, "TXT")]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout, Exception):
        return []


def _has(name: str, rtype: str) -> bool:
    import dns.exception
    import dns.resolver
    r = dns.resolver.Resolver()
    r.lifetime = 2
    try:
        return bool(list(r.resolve(name, rtype)))
    except Exception:
        return False


def _fetch(url: str, max_bytes: int = 200_000):
    return seo_check._get(url, max_bytes=max_bytes)


# --------------------------------------------------------------------------- Prüfung
def analyze(raw: str) -> dict:
    host = hostname_from(raw)
    _assert_public(host)
    org = org_domain(host)
    F: list[dict] = []

    # ---- HTTP(S) abrufen (Startseite) und Weiterleitung von http
    try:
        resp, _, elapsed, final = _fetch(f"https://{host}/")
        https_ok = True
    except SeoCheckError as exc:
        resp, final, https_ok, https_err = None, "", False, str(exc)
    hdr = {k.lower(): v for k, v in (resp.headers.items() if resp is not None else [])}
    cookies: list[str] = []
    if resp is not None:
        try:
            cookies = resp.raw.headers.getlist("Set-Cookie")
        except Exception:
            c = resp.headers.get("Set-Cookie")
            cookies = [c] if c else []

    # ---- TLS
    tls = _tls(host)
    F.append(_f("https", TLS, "ok" if https_ok and tls.get("reachable") else "fail", "HTTPS erreichbar",
                "Die Seite ist über HTTPS erreichbar." if https_ok and tls.get("reachable")
                else f"HTTPS nicht nutzbar: {tls.get('error') or (https_err if not https_ok else '')}", 10,
                "HTTPS mit gültigem Zertifikat einrichten (z. B. kostenlos über Let's Encrypt)."))
    if tls.get("reachable"):
        F.append(_f("cert", TLS, "ok" if tls.get("valid") else "fail", "Zertifikat gültig und passend zur Domain",
                    "Zertifikat wird von Browsern akzeptiert." + (f" Aussteller: {tls['issuer']}." if tls.get("issuer") else "")
                    if tls.get("valid") else f"Zertifikat wird nicht akzeptiert: {tls.get('error', 'unbekannter Fehler')}", 12,
                    "Ein gültiges, zur Domain passendes Zertifikat installieren und die Kette vollständig ausliefern."))
        if tls.get("not_after"):
            days = int((tls["not_after"] - time.time()) // 86400)
            F.append(_f("cert_exp", TLS, "ok" if days > 30 else "warn" if days > 7 else "fail", "Laufzeit des Zertifikats",
                        f"Läuft in {days} Tagen ab." if days >= 0 else f"Seit {-days} Tagen abgelaufen.", 6,
                        "Automatische Erneuerung einrichten und überwachen."))
        ver = tls.get("version", "")
        F.append(_f("tls_ver", TLS, "ok" if ver in ("TLSv1.3", "TLSv1.2") else "fail", "TLS-Version",
                    f"Ausgehandelt: {ver}." if ver else "Nicht ermittelt.", 6,
                    "Nur TLS 1.2 und 1.3 zulassen; ältere Protokolle abschalten."))
    try:
        r2, _, _, f2 = _fetch(f"http://{host}/", max_bytes=20_000)
        redirected = urlparse(f2).scheme == "https"
    except SeoCheckError:
        redirected = False
    F.append(_f("redir", TLS, "ok" if redirected else "warn", "Weiterleitung von http auf https",
                "Aufrufe über http werden auf https umgeleitet." if redirected else "http-Aufrufe landen nicht auf https.", 5,
                "Alle http-Anfragen dauerhaft (301) auf https umleiten."))

    # ---- Header
    if resp is not None:
        hsts = hdr.get("strict-transport-security", "")
        m = re.search(r"max-age=(\d+)", hsts)
        age = int(m.group(1)) if m else 0
        F.append(_f("hsts", HDR, "ok" if age >= 15552000 else "warn" if hsts else "fail", "HSTS (erzwingt HTTPS im Browser)",
                    f"max-age={age} Sekunden{', inkl. Subdomains' if 'includesubdomains' in hsts.lower() else ''}." if hsts
                    else "Header fehlt.", 6, "Strict-Transport-Security mit max-age von mindestens 6 Monaten setzen."))
        csp = hdr.get("content-security-policy", "")
        F.append(_f("csp", HDR, "ok" if csp else "warn", "Content-Security-Policy", "Gesetzt." if csp else "Nicht gesetzt.", 5,
                    "Eine Content-Security-Policy gegen das Einschleusen fremder Skripte einführen (erst im Report-Only-Modus testen)."))
        frame = "x-frame-options" in hdr or "frame-ancestors" in csp.lower()
        F.append(_f("frame", HDR, "ok" if frame else "warn", "Schutz vor Einbettung (Clickjacking)",
                    "Gesetzt." if frame else "Weder X-Frame-Options noch frame-ancestors vorhanden.", 3,
                    "X-Frame-Options: DENY (oder SAMEORIGIN) bzw. CSP frame-ancestors setzen."))
        F.append(_f("xcto", HDR, "ok" if "x-content-type-options" in hdr else "warn", "X-Content-Type-Options",
                    "Gesetzt." if "x-content-type-options" in hdr else "Nicht gesetzt.", 2, "X-Content-Type-Options: nosniff setzen."))
        F.append(_f("refpol", HDR, "ok" if "referrer-policy" in hdr else "warn", "Referrer-Policy",
                    "Gesetzt." if "referrer-policy" in hdr else "Nicht gesetzt.", 2,
                    "Referrer-Policy: strict-origin-when-cross-origin setzen."))
        F.append(_f("permpol", HDR, "ok" if "permissions-policy" in hdr else "info", "Permissions-Policy",
                    "Gesetzt." if "permissions-policy" in hdr else "Nicht gesetzt (empfohlen).", 1,
                    "Nicht benötigte Browser-Funktionen (Kamera, Mikrofon, Standort) per Permissions-Policy sperren."))
        leak = [f"{k}: {v}" for k, v in hdr.items() if k in ("server", "x-powered-by", "x-aspnet-version") and re.search(r"\d+\.\d+", v)]
        F.append(_f("leak", HDR, "warn" if leak else "ok", "Keine Software-Versionen im Header",
                    "Preisgegeben: " + "; ".join(leak) if leak else "Keine Versionsnummern in Server-Headern erkannt.", 3,
                    "Versionsangaben in Server- und X-Powered-By-Headern entfernen."))

    # ---- Cookies
    if cookies:
        def flag(c, name):
            return re.search(rf"(?i);\s*{name}\b", c) is not None
        total = len(cookies)
        no_secure = sum(1 for c in cookies if not flag(c, "secure"))
        no_http = sum(1 for c in cookies if not flag(c, "httponly"))
        no_same = sum(1 for c in cookies if not flag(c, "samesite"))
        weak = sum(1 for c in cookies if not (flag(c, "secure") and flag(c, "httponly") and flag(c, "samesite")))
        F.append(_f("cookies", COO, "ok" if weak == 0 else "warn" if weak < total else "fail", "Cookie-Schutzattribute",
                    f"{total} Cookie(s): {no_secure}× ohne Secure, {no_http}× ohne HttpOnly, {no_same}× ohne SameSite.", 6,
                    "Cookies mit Secure, HttpOnly (falls ohne Skriptzugriff) und SameSite=Lax/Strict setzen."))
    else:
        F.append(_f("cookies", COO, "info", "Cookie-Schutzattribute", "Beim ersten Aufruf werden keine Cookies gesetzt.", 0))

    # ---- E-Mail-Sicherheit (DNS)
    spf_recs = [t for t in _txt(host) + (_txt(org) if org != host else []) if t.lower().startswith("v=spf1")]
    spf = spf_recs[0] if spf_recs else ""
    if not spf:
        F.append(_f("spf", MAIL, "fail", "SPF", "Kein SPF-Eintrag: Fremde können leichter in deinem Namen Mails versenden.", 6,
                    "SPF-Eintrag (TXT, „v=spf1 … -all“) mit den erlaubten Versendern anlegen."))
    else:
        end = spf.split()[-1].lower()
        st = "ok" if end == "-all" else "warn" if end == "~all" else "fail"
        F.append(_f("spf", MAIL, st, "SPF", f"Eintrag vorhanden, Abschluss „{end}“." + (" Empfohlen ist „-all“." if st == "warn" else ""), 6,
                    "SPF strikt mit „-all“ (oder mindestens „~all“) abschließen; „+all“ und „?all“ schützen nicht."))
    dmarc_recs = [t for t in _txt(f"_dmarc.{host}") + (_txt(f"_dmarc.{org}") if org != host else []) if t.lower().startswith("v=dmarc1")]
    if not dmarc_recs:
        F.append(_f("dmarc", MAIL, "fail", "DMARC", "Kein DMARC-Eintrag.", 8,
                    "DMARC-Eintrag anlegen (zuerst p=none mit Berichten, dann p=quarantine oder p=reject)."))
    else:
        pol = re.search(r"(?i)\bp=(\w+)", dmarc_recs[0])
        pol = (pol.group(1).lower() if pol else "none")
        F.append(_f("dmarc", MAIL, "ok" if pol in ("quarantine", "reject") else "warn", "DMARC",
                    f"Richtlinie p={pol}." + (" Nur Beobachtung, kein Schutz." if pol == "none" else ""), 8,
                    "Nach der Beobachtungsphase auf p=quarantine bzw. p=reject umstellen."))
    # Domain ohne Mailverkehr (kein MX, SPF nur „-all“): richtig abgesichert, DKIM überflüssig
    no_mail = not _has(org, "MX") and spf.lower().split() == ["v=spf1", "-all"]
    dkim = None if no_mail else next((s for s in DKIM_SELECTORS if any("p=" in t for t in _txt(f"{s}._domainkey.{org}"))), None)
    if no_mail:
        F.append(_f("dkim", MAIL, "ok", "DKIM", "Nicht nötig: Die Domain empfängt und versendet keine E-Mails (kein MX, "
                    "SPF „-all“) und ist damit gegen Missbrauch als Absender vorbildlich abgesichert.", 3))
    else:
        F.append(_f("dkim", MAIL, "ok" if dkim else "info", "DKIM",
                    f"Schlüssel gefunden (Selektor „{dkim}“)." if dkim else "Über gängige Selektoren kein Schlüssel "
                    "gefunden — das beweist nichts, da Selektoren frei wählbar sind. Bitte beim Mailanbieter prüfen.", 3,
                    "DKIM beim Mailanbieter aktivieren und den Schlüssel im DNS veröffentlichen."))

    # ---- DNS und Kontakt
    F.append(_f("dnssec", DNS, "ok" if _has(org, "DS") else "warn", "DNSSEC", "Signiert (DS-Eintrag vorhanden)."
                if _has(org, "DS") else "Nicht aktiviert.", 3, "DNSSEC beim Domain-Registrar bzw. DNS-Anbieter aktivieren."))
    has_caa = _has(org, "CAA")
    F.append(_f("caa", DNS, "ok" if has_caa else "info", "CAA (erlaubte Zertifikatsaussteller)",
                "Vorhanden." if has_caa else "Nicht gesetzt (empfohlen).", 2,
                "CAA-Eintrag anlegen, damit nur gewünschte Stellen Zertifikate ausstellen dürfen."))
    sectxt = False
    try:
        r3, body, _, _ = _fetch(f"https://{host}/.well-known/security.txt", max_bytes=20_000)
        sectxt = r3.status_code == 200 and b"contact:" in body.lower()
    except SeoCheckError:
        pass
    F.append(_f("sectxt", DNS, "ok" if sectxt else "warn", "security.txt (Meldestelle für Sicherheitslücken)",
                "Vorhanden." if sectxt else "Keine /.well-known/security.txt gefunden.", 3,
                "Eine security.txt mit Kontakt für Sicherheitsmeldungen unter /.well-known/ veröffentlichen (RFC 9116)."))

    scored = [f for f in F if f["status"] != "info" and f["weight"] > 0]
    weight = sum(f["weight"] for f in scored)
    got = sum(f["weight"] * (1 if f["status"] == "ok" else 0.5 if f["status"] == "warn" else 0) for f in scored)
    return {"url": f"https://{host}", "score": round(100 * got / weight) if weight else 0, "findings": F,
            "facts": {"host": host, "domain": org, "tls": tls.get("version", ""), "aussteller": tls.get("issuer", "")}}


_SYSTEM = ("Du bist Berater für IT-Sicherheit bei {CLUB} und schreibst einen Prüfbericht für Unternehmer:innen ohne "
           "Technik-Vorwissen. Ton: ruhig, klar, sachlich, Deutsch, du-Form. Grundlage ist ausschließlich eine PASSIVE Prüfung "
           "öffentlich sichtbarer Einstellungen (TLS, HTTP-Header, Cookies, E-Mail-DNS). Sage klar, dass das KEIN Penetrationstest "
           "und keine Schwachstellenprüfung ist und Systeme, Software-Stände und Zugänge nicht geprüft wurden. Keine Rechtsberatung. "
           "Nutze NUR die Prüfergebnisse (erfinde nichts). Ordne Maßnahmen nach Risiko (kritisch zuerst), dann nach Wirkung geteilt "
           "durch Aufwand; erkläre jede Maßnahme verständlich und sage, wer sie umsetzt (z. B. Hoster, Webagentur, Mailanbieter). "
           "Bezüge nur: Art. 32 DSGVO (Sicherheit der Verarbeitung) und allgemein NIS2-Anforderungen an technische Maßnahmen; keine "
           "Paragrafen erfinden, keine Bußgelder nennen. Antworte NUR als JSON: {\"zusammenfassung\": \"3–4 Sätze\", \"massnahmen\": "
           "[{\"prio\": 1, \"titel\": \"…\", \"warum\": \"1–2 Sätze\", \"so_gehts\": \"1–3 Sätze, wer was tut\", \"aufwand\": "
           "\"gering|mittel|hoch\", \"wirkung\": \"hoch|mittel|niedrig\"}] (je echte Schwäche aus 'schwaechen' ein Eintrag, "
           "höchstens 7; Punkte aus 'hinweise' sind KEINE Schwächen – höchstens als letzter Eintrag mit Wirkung niedrig und "
           "der Formulierung ‚prüfen lassen‘; nie behaupten, dass etwas fehlt, wenn es nur nicht erkennbar war), "
           "\"keyword_hinweise\": "
           "\"\", \"naechste_schritte\": \"2 Sätze, inkl. Hinweis, dass ein Penetrationstest tiefer prüft\"}. "
           "WICHTIG für gültiges JSON: In Textwerten keine doppelten Anführungszeichen (nutze ‚einfache‘), keine Zeilenumbrüche.")


def build_report(result: dict) -> dict:
    pick = lambda st: [{k: f[k] for k in ("cat", "status", "title", "detail", "fix")}  # noqa: E731
                       for f in result["findings"] if f["status"] in st]
    payload = {"domain": result["facts"]["host"], "score": result["score"], "erkannt": result["facts"],
               "schwaechen": pick(("fail", "warn")), "hinweise": pick(("info",))}
    rep = seo_check.ai_report(_SYSTEM, payload, result)
    rep["keyword_hinweise"] = ""
    if rep.get("ai") and not payload["schwaechen"]:
        # Keine Schwächen: optionale Hinweise nie als dringende Maßnahme darstellen (Live-Test: CAA mit „Wirkung hoch“)
        for a in rep["massnahmen"]:
            a["wirkung"] = "niedrig"
            if not a["titel"].lower().startswith("optional"):
                a["titel"] = "Optional: " + a["titel"]
    if not rep["ai"]:
        rep["naechste_schritte"] = ("Setze die Maßnahmen der Reihe nach um (vieles erledigt dein Hoster oder Mailanbieter in "
                                    "wenigen Minuten) und prüfe danach erneut. Ein Penetrationstest prüft deutlich tiefer.")
    return rep
