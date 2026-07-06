"""
comunes.py — Helpers que comparten varios scripts del proyecto:
comparación de títulos, duraciones, descarga de tapas y manejo de
tonalidades (keys). Acá no hay nada que correr directamente.
"""

import difflib
import re
from pathlib import Path

import requests

import config

COVERS_DIR = Path(__file__).parent / config.COVERS_DIR


def normalizar(texto):
    """Baja un título a minúsculas y letras/números solos, para poder
    comparar "Concrete Jungle (Juaan Remix)" con
    "Concrete Jungle - Juaan Remix" sin que la puntuación moleste."""
    return re.sub(r"[^a-z0-9]", "", (texto or "").lower())


def se_parecen(a, b, umbral=0.65):
    """True si dos títulos normalizados son iguales, uno contiene al
    otro (Discogs suele agregar "EP"), o se parecen bastante."""
    na, nb = normalizar(a), normalizar(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= umbral


# Rango de BPM esperable en tu colección (música de club). Los
# detectores de tempo — y a veces la propia ficha de Beatport —
# devuelven el doble o la mitad del tempo real (67 en un track de
# 134): todo BPM automático se acomoda a este rango antes de
# guardarse. Si tu colección es de otro palo (hip hop, ambient...),
# ajustá estos dos números.
BPM_MINIMO = 88
BPM_MAXIMO = 176


def acomodar_al_rango(bpm):
    """Corrige los errores de "doble o mitad de tempo": dobla o parte
    a la mitad hasta caer en el rango esperado."""
    if not bpm:
        return None
    while bpm < BPM_MINIMO:
        bpm *= 2
    while bpm > BPM_MAXIMO:
        bpm /= 2
    return round(bpm, 1)


def parsear_duracion(texto):
    """Convierte "6:30" (o "1:02:15") a segundos. Devuelve None si
    no hay duración cargada."""
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
    """Convierte 225 segundos a "3:45", como las guarda Discogs."""
    segundos = int(round(segundos))
    return f"{segundos // 60}:{segundos % 60:02d}" if segundos else ""


def bajar_tapa(url, release_id):
    """Baja la imagen de tapa y devuelve la ruta relativa guardada
    (ej: "covers/12345.jpg"), o None si falló. El CDN de imágenes de
    Discogs rechaza pedidos sin User-Agent "de verdad", así que
    mandamos siempre el nuestro (a los demás no les molesta)."""
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
# Tonalidades (keys)
# =========================================================
# En la base guardamos la key en notación musical corta ("Am", "F#",
# "Bb"), y en la etiqueta la mostramos en notación Camelot ("8A"),
# que es la que se usa para mezclar armónicamente.

_SEMITONO = {
    "c": 0, "b#": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3,
    "e": 4, "fb": 4, "f": 5, "e#": 5, "f#": 6, "gb": 6, "g": 7,
    "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10, "b": 11, "cb": 11,
}

# Nombre canónico y código Camelot de cada tonalidad, por semitono.
# (Usamos la grafía de la rueda Camelot: Ebm y no D#m, F# y no Gb.)
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
    """Entiende una tonalidad escrita como la manda Beatport
    ("A Minor", "F♯ Major"), como la escribe un humano ("Am", "f#",
    "bb minor") o en Camelot ("8A", "12b"), y la devuelve en notación
    musical corta canónica ("Am", "F#", "Bb"). None si no se entiende."""
    if not texto:
        return None
    texto = str(texto).strip().replace("♯", "#").replace("♭", "b")

    # ¿Vino en Camelot? ("8A", "12b")
    m = re.fullmatch(r"(\d{1,2})\s*([ABab])", texto)
    if m:
        return _DESDE_CAMELOT.get(m.group(1).lstrip("0") + m.group(2).upper())

    # Notación musical: nota + modo opcional ("A Minor", "F# maj", "Am").
    m = re.fullmatch(r"([A-Ga-g][#b]?)\s*(minor|min|major|maj|m)?\.?", texto, re.IGNORECASE)
    if not m:
        return None
    semitono = _SEMITONO.get(m.group(1).lower())
    if semitono is None:
        return None
    es_menor = (m.group(2) or "").lower() in ("m", "min", "minor")
    return (_MENOR if es_menor else _MAYOR)[semitono][0]


def a_camelot(key):
    """Convierte una key musical a Camelot: "Am" -> "8A". Devuelve ""
    si no hay key o no se entiende."""
    normal = normalizar_key(key)
    if not normal:
        return ""
    es_menor = normal.endswith("m")
    semitono = _SEMITONO[(normal[:-1] if es_menor else normal).lower()]
    return (_MENOR if es_menor else _MAYOR)[semitono][1]
