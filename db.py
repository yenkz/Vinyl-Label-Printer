"""
db.py — Manejo de la base de datos local.

Usamos SQLite porque viene incluido en Python (no hay que instalar
nada) y guarda todo en un solo archivo: vinyl_labels.db, que se crea
en esta misma carpeta la primera vez que corrés algo.

Pensalo como una versión mini de una hoja de cálculo con tres "tabs":
  - releases:    un renglón por cada vinilo (LP)
  - tracks:      un renglón por cada canción de cada vinilo
  - bpm_sources: qué BPM dijo CADA fuente para cada track (Beatport,
                 la medición de YouTube, Deezer...). El BPM "elegido"
                 vive en tracks.bpm; acá queda el detalle completo,
                 que el editor muestra para que valides con criterio.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl_labels.db"


def get_connection():
    """Abre (o crea si no existe) la base de datos."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # permite acceder a columnas por nombre, ej: row["title"]
    return conn


def init_db():
    """
    Crea las tablas si todavía no existen. Es seguro correr esto
    las veces que quieras: si ya existen, no hace nada.
    """
    conn = get_connection()
    # Si la tabla de fuentes todavía no existe, después de crearla la
    # sembramos con lo que ya haya en tracks (ver más abajo).
    tablas = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS releases (
            release_id   INTEGER PRIMARY KEY,   -- ID del disco en Discogs
            artist       TEXT,
            title        TEXT,
            year         INTEGER,
            sello        TEXT,     -- sello discográfico (record label), de Discogs
            catno        TEXT,     -- número de catálogo (ej: "DDC005"), de Discogs
            released     TEXT,     -- fecha de edición del vinilo ("2024-12-30"), de Discogs
            cover_path   TEXT      -- ruta local de la tapa bajada (covers/<id>.jpg)
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id        INTEGER NOT NULL,
            position          TEXT,     -- "A1", "A2", "B1", etc.
            title             TEXT,
            artist            TEXT,     -- solo si difiere del artista del disco (discos "Various")
            duration_display  TEXT,     -- "3:45" tal cual lo entrega Discogs
            bpm               REAL,     -- vacío (NULL) hasta que se complete
            bpm_source        TEXT,     -- "api", "manual", o NULL
            bpm_alt           REAL,     -- candidato alternativo, si los medidores no coincidieron
            bpm_needs_review  INTEGER DEFAULT 0,  -- 1 = revisalo a mano en edit_bpm.py
            bpm_verified      INTEGER DEFAULT 0,  -- 1 = validado (detectores de acuerdo, auditado, o confirmado por vos)
            key               TEXT,     -- tonalidad ("Am", "F#"); en la etiqueta sale en Camelot ("8A")
            key_source        TEXT,     -- "beatport", "manual", o NULL
            isrc              TEXT,     -- código ISRC del track (de Spotify o Beatport); no se imprime
            FOREIGN KEY (release_id) REFERENCES releases(release_id)
        );

        CREATE TABLE IF NOT EXISTS bpm_sources (
            track_id  INTEGER NOT NULL,
            source    TEXT NOT NULL,    -- "beatport", "youtube", "deezer", "getsongbpm", "manual"
            bpm       REAL,             -- NULL = la fuente se consultó y no tenía el track
            detail    TEXT,             -- de dónde salió exactamente (ej: título del video medido)
            PRIMARY KEY (track_id, source),
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        );
        """
    )
    # Migración: si la base ya existía de antes de que agregáramos estas
    # columnas, las sumamos ahora.
    columnas = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "artist" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN artist TEXT")
    if "bpm_alt" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_alt REAL")
    if "bpm_needs_review" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_needs_review INTEGER DEFAULT 0")
    if "bpm_verified" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_verified INTEGER DEFAULT 0")
        # Los BPM cargados a mano ya los validaste vos al escribirlos.
        conn.execute("UPDATE tracks SET bpm_verified = 1 WHERE bpm_source = 'manual'")
    if "isrc" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN isrc TEXT")
    if "key" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN key TEXT")
    if "key_source" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN key_source TEXT")
    columnas_releases = {row["name"] for row in conn.execute("PRAGMA table_info(releases)")}
    for columna in ("sello", "catno", "released", "cover_path"):
        if columna not in columnas_releases:
            conn.execute(f"ALTER TABLE releases ADD COLUMN {columna} TEXT")
    # Migración: la primera vez que aparece bpm_sources, anotamos ahí
    # los BPM que ya estaban en tracks, cada uno bajo su fuente.
    if "bpm_sources" not in tablas:
        conn.execute(
            "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm)"
            " SELECT id, bpm_source, bpm FROM tracks"
            " WHERE bpm IS NOT NULL AND bpm_source IS NOT NULL"
        )
    conn.commit()
    conn.close()


def registrar_bpm_fuente(conn, track_id, fuente, bpm, detalle=None):
    """Anota (o actualiza) qué BPM dijo una fuente para un track.
    bpm en None significa "la consulté y no tenía el track" — sirve
    para que enrich_beatport.py no vuelva a preguntar por él.
    No hace commit: eso queda a cargo del que llama."""
    conn.execute(
        "INSERT INTO bpm_sources (track_id, source, bpm, detail) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(track_id, source) DO UPDATE SET bpm = excluded.bpm, detail = excluded.detail",
        (track_id, fuente, bpm, detalle),
    )


if __name__ == "__main__":
    # Esto te permite correr "python db.py" para chequear que
    # la base de datos se crea bien, sin tener que hacer nada más.
    init_db()
    print(f"Base de datos lista en: {DB_PATH}")
