"""
comunes.py — Shared helpers used by multiple project scripts:
title comparison, duration parsing, cover downloads, and key (tonality) handling.
Nothing here is meant to be run directly.
"""

import difflib
import re
from pathlib import Path

import requests

import config

COVERS_DIR = Path(__file__).parent / config.COVERS_DIR


def normalizar(texto):
    """Normalizes a title to lowercase with only letters/numbers, allowing
    comparison of "Concrete Jungle (Juaan Remix)" with
    "Concrete Jungle - Juaan Remix" without punctuation issues."""
    return re.sub(r"[^a-z0-9]", "", (texto or "").lower())


def se_parecen(a, b, umbral=0.65):
    """Returns True if two normalized titles are equal, one contains the
    other (Discogs often adds "EP"), or are similar enough."""
    na, nb = normalizar(a), normalizar(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= umbral


# Expected BPM range in your collection (club music). Tempo detectors —
# and sometimes Beatport itself — return double or half the actual tempo
# (67 for a 134 BPM track): all automatic BPM is fitted to this range before
# saving. If your collection is a different genre (hip hop, ambient...),
# adjust these two numbers.
BPM_MINIMO = 88
BPM_MAXIMO = 176


def acomodar_al_rango(bpm):
    """Corrects "double or half tempo" errors: doubles or halves the BPM
    until it falls within the expected range."""
    if not bpm:
        return None
    while bpm < BPM_MINIMO:
        bpm *= 2
    while bpm > BPM_MAXIMO:
        bpm /= 2
    return round(bpm, 1)


def parsear_duracion(texto):
    """Converts "6:30" (or "1:02:15") to seconds. Returns None if
    no duration is provided."""
    if not texto or ":" not in texto:
        return None
    try:
        partes = [int(p) for p in texto.split(":")]
    except ValueError:
        return None
    segundos = 0
    for p in partes:
        segundos = segundos * 60 + p
    return segundos or None


def formatear_duracion(segundos):
    """Converts 225 seconds to "3:45", as Discogs stores it."""
    segundos = int(round(segundos))
    return f"{segundos // 60}:{segundos % 60:02d}" if segundos else ""


def bajar_tapa(url, release_id):
    """Downloads the cover image and returns the saved relative path
    (e.g., "covers/12345.jpg"), or None if it failed. Discogs' image CDN
    rejects requests without a real User-Agent, so we always send ours
    (other endpoints don't mind)."""
    try:
        resp = requests.get(url, headers={"User-Agent": config.DISCOGS_USER_AGENT}, timeout=20)
        if resp.status_code != 200 or not resp.content:
            return None
    except requests.RequestException:
        return None
    COVERS_DIR.mkdir(exist_ok=True)
    destino = COVERS_DIR / f"{release_id}.jpg"
    destino.write_bytes(resp.content)
    return f"{config.COVERS_DIR}/{release_id}.jpg"


# =========================================================
# Keys (Tonalities)
# =========================================================
# In the database we store the key in short musical notation ("Am", "F#",
# "Bb"), and on the label we show it in Camelot notation ("8A"),
# which is used for harmonic mixing.

_SEMITONO = {
    "c": 0, "b#": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3,
    "e": 4, "fb": 4, "f": 5, "e#": 5, "f#": 6, "gb": 6, "g": 7,
    "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10, "b": 11, "cb": 11,
}

# Canonical name and Camelot code for each key, by semitone.
# (We use the Camelot wheel notation: Ebm not D#m, F# not Gb.)
_MENOR = {
    0: ("Cm", "5A"), 1: ("C#m", "12A"), 2: ("Dm", "7A"), 3: ("Ebm", "2A"),
    4: ("Em", "9A"), 5: ("Fm", "4A"), 6: ("F#m", "11A"), 7: ("Gm", "6A"),
    8: ("Abm", "1A"), 9: ("Am", "8A"), 10: ("Bbm", "3A"), 11: ("Bm", "10A"),
}
_MAYOR = {
    0: ("C", "8B"), 1: ("Db", "3B"), 2: ("D", "10B"), 3: ("Eb", "5B"),
    4: ("E", "12B"), 5: ("F", "7B"), 6: ("F#", "2B"), 7: ("G", "9B"),
    8: ("Ab", "4B"), 9: ("A", "11B"), 10: ("Bb", "6B"), 11: ("B", "1B"),
}
_DESDE_CAMELOT = {
    camelot: nombre for tabla in (_MENOR, _MAYOR) for nombre, camelot in tabla.values()
}


def normalizar_key(texto):
    """Understands a key written as Beatport sends it
    ("A Minor", "F♯ Major"), as a human would write it ("Am", "f#",
    "bb minor") or in Camelot ("8A", "12b"), and returns it in canonical
    short musical notation ("Am", "F#", "Bb"). None if not recognized."""
    if not texto:
        return None
    texto = str(texto).strip().replace("♯", "#").replace("♭", "b")

    # Camelot format? ("8A", "12b")
    m = re.fullmatch(r"(\d{1,2})\s*([ABab])", texto)
    if m:
        return _DESDE_CAMELOT.get(m.group(1).lstrip("0") + m.group(2).upper())

    # Musical notation: note + optional mode ("A Minor", "F# maj", "Am").
    m = re.fullmatch(r"([A-Ga-g][#b]?)\s*(minor|min|major|maj|m)?\.?", texto, re.IGNORECASE)
    if not m:
        return None
    semitono = _SEMITONO.get(m.group(1).lower())
    if semitono is None:
        return None
    es_menor = (m.group(2) or "").lower() in ("m", "min", "minor")
    return (_MENOR if es_menor else _MAYOR)[semitono][0]


def a_camelot(key):
    """Converts a musical key to Camelot: "Am" -> "8A". Returns ""
    if there's no key or it's not recognized."""
    normal = normalizar_key(key)
    if not normal:
        return ""
    es_menor = normal.endswith("m")
    semitono = _SEMITONO[(normal[:-1] if es_menor else normal).lower()]
    return (_MENOR if es_menor else _MAYOR)[semitono][1]
