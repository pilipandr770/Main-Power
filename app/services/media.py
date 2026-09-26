"""Profilfotos: Upload prüfen, neu kodieren (entfernt EXIF/GPS und eingebettete Inhalte), quadratisch zuschneiden."""
from __future__ import annotations

import io
import logging
import secrets
from pathlib import Path

from flask import current_app

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
SIZE = 512  # Kantenlänge in px
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class PhotoError(Exception):
    """Nutzerverständliche Fehlermeldung (Deutsch)."""


def avatar_dir() -> Path:
    d = Path(current_app.config.get("UPLOAD_DIR") or Path(current_app.instance_path) / "uploads") / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_avatar(fileobj) -> str:
    """Speichert das Bild als JPEG 512x512 und liefert den (zufälligen) Dateinamen."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    data = fileobj.read(MAX_BYTES + 1)
    if not data:
        raise PhotoError("Bitte wähle eine Bilddatei aus.")
    if len(data) > MAX_BYTES:
        raise PhotoError("Das Foto ist größer als 5 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise PhotoError("Bitte lade ein JPG-, PNG- oder WebP-Bild hoch.")
        img.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise PhotoError("Diese Datei konnte nicht als Bild gelesen werden.")
    img = ImageOps.exif_transpose(img)  # Drehung aus EXIF anwenden, danach werden alle Metadaten verworfen
    img = ImageOps.fit(img.convert("RGB"), (SIZE, SIZE), Image.LANCZOS)
    name = secrets.token_hex(12) + ".jpg"
    img.save(avatar_dir() / name, "JPEG", quality=86, optimize=True)  # ohne exif=
    return name


def delete_avatar(name: str | None) -> None:
    if not name or "/" in name or "\\" in name or ".." in name:
        return
    try:
        (avatar_dir() / name).unlink(missing_ok=True)
    except OSError:
        log.exception("Foto %s konnte nicht gelöscht werden", name)
