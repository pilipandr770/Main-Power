"""Embeddings mit austauschbarem Provider.

auto  -> voyage (wenn VOYAGE_API_KEY) -> openai (wenn OPENAI_API_KEY) -> local
local -> deterministisches Hashing (Wort + Zeichen-Trigramme), offline, ohne Kosten.
        Gut genug für Demo/Tests; für Produktion Voyage (multilingual) empfohlen.
"""
from __future__ import annotations

import hashlib
import logging
import re

import numpy as np
import requests
from flask import current_app

log = logging.getLogger(__name__)
LOCAL_DIM = 1024

_STOP = set("""
aber alle als also am an auch auf aus bei bin bis bist da damit dann das dass dem den der des die dir
doch du ein eine einem einen einer eines er es für hab habe haben hat ich ihr im in ist ja jede jeden
kann kein man mehr mein meine mich mir mit nach nicht noch nur oder sich sie sind so über um und uns
unser vom von vor war was weil wenn wer wie wir wird zu zum zur the and for with you are
""".split())


def provider_name() -> str:
    p = current_app.config.get("EMBEDDING_PROVIDER", "auto")
    if p != "auto":
        return p
    if current_app.config.get("VOYAGE_API_KEY"):
        return "voyage"
    if current_app.config.get("OPENAI_API_KEY"):
        return "openai"
    return "local"


def _stem(w: str) -> str:
    for suf in ("ungen", "ung", "innen", "erin", "ern", "en", "er", "es", "e", "n", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _local_embed(text: str) -> list[float]:
    vec = np.zeros(LOCAL_DIM, dtype=np.float32)
    words = [w for w in re.findall(r"[a-zäöüß0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1]
    feats: list[tuple[str, float]] = []
    for w in words:
        s = _stem(w)
        feats.append(("w:" + s, 1.0))
        padded = f"#{s}#"
        for i in range(len(padded) - 2):
            feats.append(("c:" + padded[i:i + 3], 0.35))
    for f, weight in feats:
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        vec[h % LOCAL_DIM] += weight if (h >> 20) & 1 else -weight
    n = np.linalg.norm(vec)
    return (vec / n).tolist() if n else vec.tolist()


def _voyage(texts: list[str], input_type: str) -> list[list[float]]:
    r = requests.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {current_app.config['VOYAGE_API_KEY']}"},
        json={"input": texts, "model": current_app.config["VOYAGE_MODEL"], "input_type": input_type},
        timeout=30,
    )
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def _openai(texts: list[str]) -> list[list[float]]:
    r = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {current_app.config['OPENAI_API_KEY']}"},
        json={"input": texts, "model": current_app.config["OPENAI_EMBED_MODEL"]},
        timeout=30,
    )
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def embed(texts: list[str], input_type: str = "document") -> tuple[list[list[float]], str]:
    """Gibt (Vektoren, Providername) zurück. Fällt bei Fehlern auf local zurück."""
    texts = [t if t and t.strip() else "-" for t in texts]
    p = provider_name()
    try:
        if p == "voyage":
            return _voyage(texts, input_type), f"voyage:{current_app.config['VOYAGE_MODEL']}"
        if p == "openai":
            return _openai(texts), f"openai:{current_app.config['OPENAI_EMBED_MODEL']}"
    except Exception:
        log.exception("Embedding-Provider %s fehlgeschlagen, nutze local", p)
    return [_local_embed(t) for t in texts], "local:v1"


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    return float(va @ vb / (na * nb)) if na and nb else 0.0
