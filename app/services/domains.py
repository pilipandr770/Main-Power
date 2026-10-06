"""Eigene Domains für Klubs: hinzufügen, per DNS bestätigen, aktivieren.

Ablauf: Der Klub trägt eine Domain ein (`add_claim`). Er legt beim DNS-Anbieter zwei Einträge an: einen TXT-Eintrag mit
seinem Token (Besitznachweis) und einen CNAME/A-Eintrag auf unseren Server (Weiterleitung). Sobald der TXT-Eintrag stimmt
(`check_claim`, auf Knopfdruck oder stündlich), steht die Domain in `Club.domains` und wird bedient; ein Hilfsskript auf dem
Server (deploy/sync-traefik-hosts.sh) holt dafür das Zertifikat. Ohne Besitznachweis wird nie eine Domain aktiv.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from datetime import timedelta

from flask import current_app

from ..extensions import db
from ..models import Club, ClubDomainClaim, utcnow

log = logging.getLogger(__name__)
MAX_DOMAINS = 5            # aktive + offene je Klub
CLAIM_TTL_DAYS = 14        # offene Ansprüche verfallen
TXT_PREFIX = "_klub-verify."
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class DomainError(ValueError):
    """Nutzerverständliche Fehlermeldung (Deutsch)."""


def normalize(raw: str) -> str:
    """Eingabe → kleingeschriebener, ASCII-kodierter Hostname (IDN als Punycode). Wirft DomainError bei Unbrauchbarem."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0].strip(".")
    if not d:
        raise DomainError("Bitte gib eine Domain an, z. B. netzwerk.meinverein.de.")
    try:
        d = d.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainError("Diese Domain enthält ungültige Zeichen.") from exc
    try:
        ipaddress.ip_address(d)
        raise DomainError("Bitte eine Domain angeben, keine IP-Adresse.")
    except ValueError as exc:
        if isinstance(exc, DomainError):
            raise
    labels = d.split(".")
    if len(d) > 253 or len(labels) < 2 or not all(LABEL.match(x) for x in labels) or not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]+", labels[-1]):
        raise DomainError("Das ist keine gültige Domain. Erlaubt sind Buchstaben, Ziffern und Bindestriche, z. B. netzwerk.meinverein.de.")
    return d


def reserved_hosts() -> set[str]:
    """Domains, die Klubs nie als eigene Domain nutzen dürfen: die Plattform selbst und ihre Subdomains."""
    cfg = current_app.config
    out = set()
    for key in ("PLATFORM_DOMAIN", "PLATFORM_DNS_TARGET"):
        if cfg.get(key):
            out.add(cfg[key].lower().strip("."))
    return out | {h.strip().lower() for h in (cfg.get("PLATFORM_HOME_HOSTS") or "").split(",") if h.strip()}


def _is_platform_name(d: str) -> bool:
    base = (current_app.config.get("PLATFORM_DOMAIN") or "").lower().strip(".")
    return d in reserved_hosts() or bool(base and d.endswith("." + base))


def owner_of(domain: str) -> Club | None:
    for c in Club.query.execution_options(all_clubs=True).all():
        if domain in c.domain_list:
            return c
    return None


def add_claim(club: Club, raw: str) -> ClubDomainClaim:
    d = normalize(raw)
    if _is_platform_name(d):
        raise DomainError("Diese Domain gehört zur Plattform. Deine Adresse unter der Plattform-Domain hast du bereits.")
    owner = owner_of(d)
    if owner is not None:
        raise DomainError("Diese Domain ist dem Klub bereits zugeordnet." if owner.id == club.id
                          else "Diese Domain wird bereits von einem anderen Klub genutzt.")
    mine = ClubDomainClaim.query.filter_by(club_id=club.id).all()
    if len(mine) + len(club.domain_list) >= MAX_DOMAINS:
        raise DomainError(f"Höchstens {MAX_DOMAINS} eigene Domains je Klub. Entferne zuerst eine andere.")
    existing = next((c for c in mine if c.domain == d), None)
    if existing:
        return existing
    claim = ClubDomainClaim(club_id=club.id, domain=d, token=secrets.token_hex(16))
    db.session.add(claim)
    return claim


def txt_name(claim: ClubDomainClaim) -> str:
    return TXT_PREFIX + claim.domain


def txt_value(claim: ClubDomainClaim) -> str:
    return f"klub-verify={claim.token}"


def _resolver():
    import dns.resolver
    r = dns.resolver.Resolver(configure=True)
    r.nameservers = ["1.1.1.1", "8.8.8.8"] + [n for n in r.nameservers if n not in ("1.1.1.1", "8.8.8.8")][:1]
    r.timeout, r.lifetime = 4.0, 8.0
    return r


def lookup_txt(name: str) -> list[str]:
    """TXT-Werte eines Namens (leer, wenn es keinen Eintrag gibt)."""
    import dns.exception
    import dns.resolver
    try:
        answers = _resolver().resolve(name, "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.DNSException as exc:
        raise DomainError("Die DNS-Abfrage hat nicht geantwortet. Bitte versuch es gleich noch einmal.") from exc
    return ["".join(part.decode("utf-8", "replace") for part in rdata.strings) for rdata in answers]


def lookup_routes_to_us(domain: str) -> bool | None:
    """Zeigt die Domain auf unseren Server? True/False; None = nicht feststellbar (z. B. Proxy wie Cloudflare)."""
    import dns.exception
    import dns.resolver
    cfg = current_app.config
    ips = {i.strip() for i in (cfg.get("PLATFORM_SERVER_IP") or "").split(",") if i.strip()}
    target = (cfg.get("PLATFORM_DNS_TARGET") or "").lower().strip(".")
    try:
        if target:
            try:
                for rd in _resolver().resolve(domain, "CNAME"):
                    if str(rd.target).lower().strip(".") == target:
                        return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                pass
        found = {str(rd) for rd in _resolver().resolve(domain, "A")}
    except dns.exception.DNSException:
        return False
    if ips and found & ips:
        return True
    return None if found else False


def activate(club: Club, domain: str) -> None:
    """Domain bedienen: vorne in Club.domains (wird dann die Hauptadresse in Links und Mails)."""
    if domain not in club.domain_list:
        club.domains = ",".join([domain] + club.domain_list)


def check_claim(claim: ClubDomainClaim) -> bool:
    """Besitznachweis per TXT prüfen. True = bestätigt und aktiviert (der Anspruch ist dann gelöscht)."""
    club = db.session.get(Club, claim.club_id)
    claim.checked_at = utcnow()
    if club is None:
        db.session.delete(claim)
        return False
    owner = owner_of(claim.domain)
    if owner is not None and owner.id != club.id:
        claim.note = "Die Domain gehört inzwischen einem anderen Klub."
        return False
    try:
        values = lookup_txt(txt_name(claim))
    except DomainError as exc:
        claim.note = str(exc)
        return False
    if txt_value(claim) not in [v.strip() for v in values]:
        claim.note = ("Der TXT-Eintrag wurde noch nicht gefunden. DNS-Änderungen brauchen manchmal bis zu einer Stunde."
                      if not values else "Der TXT-Eintrag ist da, aber der Wert stimmt nicht überein.")
        return False
    activate(club, claim.domain)
    db.session.delete(claim)
    return True


def check_pending() -> tuple[int, int]:
    """Alle offenen Ansprüche prüfen, alte löschen. Gibt (bestätigt, verfallen) zurück."""
    ok = gone = 0
    cutoff = utcnow() - timedelta(days=CLAIM_TTL_DAYS)
    for claim in ClubDomainClaim.query.all():
        if claim.created_at < cutoff:
            db.session.delete(claim)
            gone += 1
        elif check_claim(claim):
            ok += 1
    db.session.commit()
    return ok, gone


def active_hosts() -> list[str]:
    """Alle eigenen Domains aktiver Klubs (für das Server-Skript, das die Zertifikate anfordert)."""
    static = {h.strip().lower() for h in (current_app.config.get("PLATFORM_STATIC_HOSTS") or "").split(",") if h.strip()}
    out: list[str] = []
    for c in Club.query.execution_options(all_clubs=True).filter_by(status="active").all():
        out += [d for d in c.domain_list if not _is_platform_name(d) and d not in static]
    return sorted(set(out))
