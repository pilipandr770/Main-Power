"""Bilder: Profilfotos und Klub-Medien (Logo, Startseite, Formate).

Jedes Bild wird neu kodiert (entfernt EXIF/GPS und eingebettete Inhalte) und bekommt einen zufälligen Dateinamen.
Ablage je Klub: <uploads>/clubs/<id>/…; der Standardklub nutzt aus Kompatibilitätsgründen weiter <uploads>/….
"""
from __future__ import annotations

import io
import logging
import secrets
from pathlib import Path

from flask import current_app

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
SIZE = 512  # Kantenlänge Profilfoto in px
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class PhotoError(Exception):
    """Nutzerverständliche Fehlermeldung (Deutsch)."""


def _base() -> Path:
    return Path(current_app.config.get("UPLOAD_DIR") or Path(current_app.instance_path) / "uploads")


def club_root() -> Path:
    from ..tenancy import current_club
    club = current_club()
    default_slug = current_app.config.get("DEFAULT_CLUB_SLUG", "klub")
    if club is None or club.slug == default_slug:
        return _base()
    return _base() / "clubs" / str(club.id)


def avatar_dir() -> Path:
    d = club_root() / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def club_media_dir() -> Path:
    d = club_root() / "club"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open(fileobj):
    from PIL import Image, ImageOps, UnidentifiedImageError

    data = fileobj.read(MAX_BYTES + 1)
    if not data:
        raise PhotoError("Bitte wähle eine Bilddatei aus.")
    if len(data) > MAX_BYTES:
        raise PhotoError("Das Bild ist größer als 5 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise PhotoError("Bitte lade ein JPG-, PNG- oder WebP-Bild hoch.")
        img.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise PhotoError("Diese Datei konnte nicht als Bild gelesen werden.")
    return ImageOps.exif_transpose(img)  # Drehung anwenden, danach werden alle Metadaten verworfen


def save_avatar(fileobj) -> str:
    """Speichert das Bild als JPEG 512x512 und liefert den (zufälligen) Dateinamen."""
    from PIL import Image, ImageOps
    img = ImageOps.fit(_open(fileobj).convert("RGB"), (SIZE, SIZE), Image.LANCZOS)
    name = secrets.token_hex(12) + ".jpg"
    img.save(avatar_dir() / name, "JPEG", quality=86, optimize=True)  # ohne exif=
    return name


def save_club_image(fileobj, kind: str) -> str:
    """Logo (PNG mit Transparenz, max. 480x160) oder Foto (JPEG, max. 1600 px). Liefert "upload:<datei>"."""
    from PIL import Image
    img = _open(fileobj)
    if kind == "logo":
        img = img.convert("RGBA")
        img.thumbnail((480, 160), Image.LANCZOS)
        name = secrets.token_hex(12) + ".png"
        img.save(club_media_dir() / name, "PNG", optimize=True)
    else:
        img = img.convert("RGB")
        img.thumbnail((1600, 1600), Image.LANCZOS)
        name = secrets.token_hex(12) + ".jpg"
        img.save(club_media_dir() / name, "JPEG", quality=84, optimize=True)
    return "upload:" + name


def _safe(name: str | None) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def delete_club_image(ref: str | None) -> None:
    if not ref or not ref.startswith("upload:"):
        return
    name = ref[len("upload:"):]
    if _safe(name):
        try:
            (club_media_dir() / name).unlink(missing_ok=True)
        except OSError:
            log.exception("Klub-Bild %s konnte nicht gelöscht werden", name)


def delete_avatar(name: str | None) -> None:
    if not _safe(name):
        return
    try:
        (avatar_dir() / name).unlink(missing_ok=True)
    except OSError:
        log.exception("Foto %s konnte nicht gelöscht werden", name)
