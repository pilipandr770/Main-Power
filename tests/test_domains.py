"""Subdomains der Plattform und eigene Domains der Klubs (DNS-Nachweis, Aktivierung, Server-Hilfen)."""
from datetime import timedelta

import pytest
from click.testing import CliRunner

from app.extensions import db
from app.models import Club, ClubDomainClaim, utcnow
from app.services import domains
from app.tenancy import use_club
from tests.test_multiclub import B_HOST, Cfg, app, client, login  # noqa: F401  (Fixtures)

HOME = "http://localhost"


def _admin(client):
    login(client, "admin@test.local", "AdminPass12345")


def _claim(client, domain):
    return client.post("/admin/domain", data={"domain": domain}, follow_redirects=True).get_data(as_text=True)


def test_normalize_accepts_hosts_and_rejects_junk():
    assert domains.normalize("https://Netzwerk.MeinVerein.de/path?x=1") == "netzwerk.meinverein.de"
    puny = domains.normalize("müller-klub.de")                                         # Umlaute als Punycode
    assert puny.startswith("xn--") and puny.isascii() and puny.endswith(".de")
    for bad in ("", "nodot", "1.2.3.4", "a..b.de", "-bad.de", "bad-.de", "*.example.de", "ex ample.de", "a.b"):
        with pytest.raises(domains.DomainError):
            domains.normalize(bad)


def test_claim_rules(client, app):
    _admin(client)
    assert "ist vorgemerkt" in _claim(client, "netzwerk.meinverein.de")
    page = client.get("/admin/domain").get_data(as_text=True)
    assert "_klub-verify.netzwerk.meinverein.de" in page and "klub-verify=" in page and "wartet auf DNS" in page
    assert "gehört zur Plattform" in _claim(client, "irgendwas.klubs.test")           # Subdomain der Plattform
    assert "gehört zur Plattform" in _claim(client, "klubs.test")
    assert "keine gültige Domain" in _claim(client, "kaputt")
    with app.app_context():
        assert ClubDomainClaim.query.count() == 1
        # Höchstzahl
        club = Club.query.filter_by(slug="klub").one()
    for i in range(domains.MAX_DOMAINS - 1):
        assert "ist vorgemerkt" in _claim(client, f"d{i}.example.de")
    assert "Höchstens" in _claim(client, "zu-viel.example.de")


def test_domain_is_activated_only_after_txt_proof(client, app, monkeypatch):
    _admin(client)
    _claim(client, "netzwerk.meinverein.de")
    with app.app_context():
        claim = ClubDomainClaim.query.one()
        cid, token = claim.id, claim.token
    seen = {}
    monkeypatch.setattr(domains, "lookup_txt", lambda name: seen.setdefault("name", name) and [])
    r = client.post(f"/admin/domain/{cid}/pruefen", follow_redirects=True)
    assert "noch nicht gefunden" in r.get_data(as_text=True) and seen["name"] == "_klub-verify.netzwerk.meinverein.de"
    monkeypatch.setattr(domains, "lookup_txt", lambda name: ["klub-verify=falsch"])
    assert "stimmt nicht überein" in client.post(f"/admin/domain/{cid}/pruefen", follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        assert Club.query.filter_by(slug="klub").one().domain_list == []
    monkeypatch.setattr(domains, "lookup_txt", lambda name: ["irgendwas", f"klub-verify={token}"])
    r = client.post(f"/admin/domain/{cid}/pruefen", follow_redirects=True)
    assert "bestätigt und aktiv" in r.get_data(as_text=True)
    with app.app_context():
        club = Club.query.filter_by(slug="klub").one()
        assert club.domain_list == ["netzwerk.meinverein.de"] and ClubDomainClaim.query.count() == 0
        assert domains.active_hosts() == ["netzwerk.meinverein.de"]
        from app.services import club as club_settings
        assert "netzwerk.meinverein.de" in club_settings.base_url(club)      # Links und Mails zeigen auf die Domain
    # Die Domain bedient jetzt genau diesen Klub, ein anderer Klub kann sie nicht beanspruchen
    assert "Die richtigen Menschen" in client.get("/", base_url="http://netzwerk.meinverein.de").get_data(as_text=True)
    client.post("/logout")
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=f"http://{B_HOST}")
    assert "bereits von einem anderen Klub" in client.post("/admin/domain", base_url=f"http://{B_HOST}",
        data={"domain": "netzwerk.meinverein.de"}, follow_redirects=True).get_data(as_text=True)
    # Entfernen: die Plattform-Adresse bleibt
    client.post("/logout", base_url=f"http://{B_HOST}")
    _admin(client)
    client.post("/admin/domain/entfernen", data={"domain": "netzwerk.meinverein.de"})
    with app.app_context():
        assert Club.query.filter_by(slug="klub").one().domain_list == []
    assert client.get("/", base_url="http://netzwerk.meinverein.de").status_code == 200      # Standardklub-Fallback
    app.config["STRICT_HOSTS"] = True
    assert client.get("/", base_url="http://netzwerk.meinverein.de").status_code == 404      # strikt: unbekannter Host


def test_claims_of_other_clubs_are_invisible(client, app):
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=f"http://{B_HOST}")
    client.post("/admin/domain", base_url=f"http://{B_HOST}", data={"domain": "berlin-klub.de"})
    with app.app_context():
        cid = ClubDomainClaim.query.one().id
    client.post("/logout", base_url=f"http://{B_HOST}")
    _admin(client)
    assert "berlin-klub.de" not in client.get("/admin/domain").get_data(as_text=True)
    assert client.post(f"/admin/domain/{cid}/pruefen").status_code == 404
    assert client.post(f"/admin/domain/{cid}/loeschen").status_code == 404


def test_only_superadmin_manages_domains(client, app):
    client.post("/registrieren", data={"first_name": "N", "last_name": "N", "email": "n@example.com",
                                       "password": "sehr-sicheres-pw", "consent_privacy": "1", "consent_values": "1"})
    assert client.get("/admin/domain").status_code in (302, 403)
    assert client.post("/admin/domain", data={"domain": "x.example.de"}).status_code in (302, 403)


def test_pending_claims_are_checked_and_expire(client, app, monkeypatch):
    with app.app_context():
        club = Club.query.filter_by(slug="klub").one()
        fresh = domains.add_claim(club, "fresh.example.de")
        old = domains.add_claim(club, "old.example.de")
        old.created_at = utcnow() - timedelta(days=domains.CLAIM_TTL_DAYS + 1)
        db.session.commit()
        token = fresh.token
        monkeypatch.setattr(domains, "lookup_txt", lambda name: [f"klub-verify={token}"] if "fresh" in name else [])
        assert domains.check_pending() == (1, 1)
        assert club.domain_list == ["fresh.example.de"] and ClubDomainClaim.query.count() == 0


def test_cli_lists_active_hosts_of_active_clubs_only(app):
    with app.app_context():
        a, b = Club.query.filter_by(slug="klub").one(), Club.query.filter_by(slug="berlin").one()
        a.domains, b.domains = "klub-a.example.de", "klub-b.example.de, klubs.test"      # Plattform-Name wird nie ausgegeben
        db.session.commit()
        runner = app.test_cli_runner()
        assert runner.invoke(args=["list-hosts"]).output.split() == ["klub-a.example.de", "klub-b.example.de"]
        b.status = "suspended"
        db.session.commit()
        assert runner.invoke(args=["list-hosts"]).output.split() == ["klub-a.example.de"]
        assert "0 bestätigt" in runner.invoke(args=["check-domains"]).output


def test_healthz_works_for_unknown_host_in_strict_mode(client, app):
    app.config["STRICT_HOSTS"] = True
    assert client.get("/healthz", base_url="http://unbekannt.example").status_code == 200
    assert client.get("/", base_url="http://unbekannt.example").status_code == 404


def test_reserved_names_cannot_be_club_slugs(client, app):
    app.config["PLATFORM_RESERVED_SLUGS"] = "botteu, rasayana"
    client.post("/plattform/login", data={"email": Cfg.PLATFORM_ADMIN_EMAIL, "password": Cfg.PLATFORM_ADMIN_PASSWORD})
    for slug in ("botteu", "shop", "www", "klubs"):
        r = client.post("/plattform/klub/neu", data={"name": "X Klub", "slug": slug, "admin_email": "x@example.com",
                                                      "admin_password": "xxxxxxxxxxxx"})
        assert "3–40 Zeichen" in r.get_data(as_text=True), slug
    with app.app_context():
        assert Club.query.filter_by(slug="botteu").count() == 0
