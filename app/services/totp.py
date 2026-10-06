"""Zwei-Faktor-Anmeldung per Authenticator-App (TOTP nach RFC 6238) und Wiederherstellungscodes.

Ohne externe TOTP-Bibliothek: HMAC-SHA1, 30-Sekunden-Schritte, 6 Stellen — kompatibel mit Google Authenticator, Microsoft
Authenticator, 1Password, Aegis usw. Das Geheimnis liegt verschlüsselt in der Datenbank (Fernet, Schlüssel aus SECRET_KEY).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from flask import current_app

STEP = 30
DIGITS = 6
RECOVERY_COUNT = 8
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne I, O, 0, 1


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return f"{number:0{DIGITS}d}"


def code_at(secret: str, now: float | None = None) -> str:
    return _hotp(secret, int((now if now is not None else time.time()) // STEP))


def verify(secret: str, code: str, after_step: int = 0, now: float | None = None, window: int = 1) -> int | None:
    """Prüft einen Code (±window Schritte Uhrabweichung). Gibt den verbrauchten Schritt zurück oder None.

    `after_step` = zuletzt akzeptierter Schritt: derselbe Code lässt sich nicht zweimal verwenden (Replay-Schutz)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = int((now if now is not None else time.time()) // STEP)
    for step in range(current - window, current + window + 1):
        if step > after_step and hmac.compare_digest(_hotp(secret, step), code):
            return step
    return None


def provisioning_uri(issuer: str, account: str, secret: str) -> str:
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&digits={DIGITS}&period={STEP}"


def qr_data_uri(text: str) -> str:
    import segno
    return segno.make(text, error="m").svg_data_uri(scale=5, border=2, dark="#000000", light="#ffffff")


# --------------------------------------------------------------------------- Geheimnis verschlüsseln
def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256((current_app.config["SECRET_KEY"] + "|totp").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def unseal(token: str) -> str | None:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# --------------------------------------------------------------------------- Wiederherstellungscodes
def _norm(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256((current_app.config["SECRET_KEY"] + "|rc|" + _norm(code)).encode()).hexdigest()


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """(Klartext-Codes zum einmaligen Anzeigen „ABCDE-FGHJK“, Hashes zum Speichern)."""
    codes = ["".join(secrets.choice(_ALPHABET) for _ in range(10)) for _ in range(RECOVERY_COUNT)]
    shown = [f"{c[:5]}-{c[5:]}" for c in codes]
    return shown, [_hash(c) for c in shown]


def use_recovery_code(hashes: list[str], code: str) -> list[str] | None:
    """Verbraucht einen Code: gibt die Restliste zurück oder None, wenn der Code nicht passt."""
    h = _hash(code)
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            return [x for x in hashes if x != stored]
    return None
