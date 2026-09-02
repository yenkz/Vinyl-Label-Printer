"""
common.py — Shared helpers used by multiple project scripts:
title comparison, duration parsing, cover downloads, and key (tonality) handling.
Nothing here is meant to be run directly.
"""

import difflib
import re
import unicodedata
from pathlib import Path

import requests

import config

COVERS_DIR = Path(__file__).parent / config.COVERS_DIR


def ascii_fold(text):
    """Replaces accented letters with their plain ASCII base ("Étienne" ->
    "Etienne"), dropping characters that have no ASCII equivalent."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()


def normalize(text):
    """Normalizes a title to lowercase with only letters/numbers, allowing
    comparison of "Concrete Jungle (Juaan Remix)" with
    "Concrete Jungle - Juaan Remix" without punctuation or accent issues."""
    return re.sub(r"[^a-z0-9]", "", ascii_fold(text).lower())


def looks_similar(a, b, threshold=0.65):
    """Returns True if two normalized titles are equal, one contains the
    other (Discogs often adds "EP"), or are similar enough."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


# Expected BPM range in your collection (club music). Tempo detectors —
# and sometimes Beatport itself — return double or half the actual tempo
# (67 for a 134 BPM track): all automatic BPM is fitted to this range before
# saving. If your collection is a different genre (hip hop, ambient...),
# adjust these two numbers.
BPM_MIN = 88
BPM_MAX = 176


def fit_to_range(bpm):
    """Corrects "double or half tempo" errors: doubles or halves the BPM
    until it falls within the expected range."""
    if not bpm:
        return None
    while bpm < BPM_MIN:
        bpm *= 2
    while bpm > BPM_MAX:
        bpm /= 2
    return round(bpm, 1)


def parse_duration(text):
    """Converts "6:30" (or "1:02:15") to seconds. Returns None if
    no duration is provided."""
    if not text or ":" not in text:
        return None
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds or None


def format_duration(seconds):
    """Converts 225 seconds to "3:45", as Discogs stores it."""
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else ""


def download_cover(url, release_id):
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
    dest = COVERS_DIR / f"{release_id}.jpg"
    dest.write_bytes(resp.content)
    return f"{config.COVERS_DIR}/{release_id}.jpg"


# =========================================================
# Keys (Tonalities)
# =========================================================
# In the database we store the key in short musical notation ("Am", "F#",
# "Bb"), and on the label we show it in Camelot notation ("8A"),
# which is used for harmonic mixing.

_SEMITONE = {
    "c": 0, "b#": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3,
    "e": 4, "fb": 4, "f": 5, "e#": 5, "f#": 6, "gb": 6, "g": 7,
    "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10, "b": 11, "cb": 11,
}

# Canonical name and Camelot code for each key, by semitone.
# (We use the Camelot wheel notation: Ebm not D#m, F# not Gb.)
_MINOR = {
    0: ("Cm", "5A"), 1: ("C#m", "12A"), 2: ("Dm", "7A"), 3: ("Ebm", "2A"),
    4: ("Em", "9A"), 5: ("Fm", "4A"), 6: ("F#m", "11A"), 7: ("Gm", "6A"),
    8: ("Abm", "1A"), 9: ("Am", "8A"), 10: ("Bbm", "3A"), 11: ("Bm", "10A"),
}
_MAJOR = {
    0: ("C", "8B"), 1: ("Db", "3B"), 2: ("D", "10B"), 3: ("Eb", "5B"),
    4: ("E", "12B"), 5: ("F", "7B"), 6: ("F#", "2B"), 7: ("G", "9B"),
    8: ("Ab", "4B"), 9: ("A", "11B"), 10: ("Bb", "6B"), 11: ("B", "1B"),
}
_FROM_CAMELOT = {
    camelot: nombre for tabla in (_MINOR, _MAJOR) for nombre, camelot in tabla.values()
}


def normalize_key(text):
    """Understands a key written as Beatport sends it
    ("A Minor", "F♯ Major"), as a human would write it ("Am", "f#",
    "bb minor") or in Camelot ("8A", "12b"), and returns it in canonical
    short musical notation ("Am", "F#", "Bb"). None if not recognized."""
    if not text:
        return None
    text = str(text).strip().replace("♯", "#").replace("♭", "b")

    # Camelot format? ("8A", "12b")
    m = re.fullmatch(r"(\d{1,2})\s*([ABab])", text)
    if m:
        return _FROM_CAMELOT.get(m.group(1).lstrip("0") + m.group(2).upper())

    # Musical notation: note + optional mode ("A Minor", "F# maj", "Am").
    m = re.fullmatch(r"([A-Ga-g][#b]?)\s*(minor|min|major|maj|m)?\.?", text, re.IGNORECASE)
    if not m:
        return None
    semitone = _SEMITONE.get(m.group(1).lower())
    if semitone is None:
        return None
    is_minor = (m.group(2) or "").lower() in ("m", "min", "minor")
    return (_MINOR if is_minor else _MAJOR)[semitone][0]


def to_camelot(key):
    """Converts a musical key to Camelot: "Am" -> "8A". Returns ""
    if there's no key or it's not recognized."""
    normal = normalize_key(key)
    if not normal:
        return ""
    is_minor = normal.endswith("m")
    semitone = _SEMITONE[(normal[:-1] if is_minor else normal).lower()]
    return (_MINOR if is_minor else _MAJOR)[semitone][1]
