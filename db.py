"""
db.py — Local database management.

We use SQLite because it comes with Python (no installation needed)
and stores everything in a single file: vinyl_labels.db, created
in this folder the first time you run something.

Think of it as a mini spreadsheet with three "tabs":
  - releases:    one row per vinyl record (LP)
  - tracks:      one row per song on each vinyl
  - bpm_sources: what BPM EACH source reported for each track (Beatport,
                 YouTube measurement, Deezer...). The "chosen" BPM
                 lives in tracks.bpm; the full details are stored here,
                 which the editor shows so you can validate intelligently.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl_labels.db"


def get_connection():
    """Opens (or creates if it doesn't exist) the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # allows accessing columns by name, e.g., row["title"]
    return conn


def init_db():
    """
    Creates the tables if they don't exist yet. It's safe to run this
    as many times as you want: if they already exist, it does nothing.
    """
    conn = get_connection()
    # If the sources table doesn't exist yet, after creating it we
    # populate it with what's already in tracks (see below).
    tablas = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS releases (
            release_id   INTEGER PRIMARY KEY,   -- Discogs record ID
            artist       TEXT,
            title        TEXT,
            year         INTEGER,
            sello        TEXT,     -- record label, from Discogs
            catno        TEXT,     -- catalog number (e.g., "DDC005"), from Discogs
            released     TEXT,     -- vinyl release date ("2024-12-30"), from Discogs
            cover_path   TEXT      -- local path to downloaded cover (covers/<id>.jpg)
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id        INTEGER NOT NULL,
            position          TEXT,     -- "A1", "A2", "B1", etc.
            title             TEXT,
            artist            TEXT,     -- only if different from record artist ("Various" records)
            duration_display  TEXT,     -- "3:45" exactly as Discogs provides it
            bpm               REAL,     -- empty (NULL) until filled
            bpm_source        TEXT,     -- "api", "manual", or NULL
            bpm_alt           REAL,     -- alternative candidate if the detectors disagreed
            bpm_needs_review  INTEGER DEFAULT 0,  -- 1 = review manually in edit_bpm.py
            bpm_verified      INTEGER DEFAULT 0,  -- 1 = validated (detectors agree, audited, or confirmed by you)
            key               TEXT,     -- tonality ("Am", "F#"); displayed on label in Camelot ("8A")
            key_source        TEXT,     -- "beatport", "manual", or NULL
            isrc              TEXT,     -- ISRC code for track (from Spotify or Beatport); not printed
            FOREIGN KEY (release_id) REFERENCES releases(release_id)
        );

        CREATE TABLE IF NOT EXISTS bpm_sources (
            track_id  INTEGER NOT NULL,
            source    TEXT NOT NULL,    -- "beatport", "youtube", "deezer", "getsongbpm", "manual"
            bpm       REAL,             -- NULL = source was consulted and didn't have the track
            detail    TEXT,             -- where it came from exactly (e.g., measured video title)
            PRIMARY KEY (track_id, source),
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        );
        """
    )
    # Migration: if the database existed before we added these
    # columns, we add them now.
    columnas = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "artist" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN artist TEXT")
    if "bpm_alt" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_alt REAL")
    if "bpm_needs_review" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_needs_review INTEGER DEFAULT 0")
    if "bpm_verified" not in columnas:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_verified INTEGER DEFAULT 0")
        # BPM entered manually were already validated by you when you wrote them.
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
    # Migration: the first time bpm_sources appears, we record there
    # the BPM that already existed in tracks, each under its source.
    if "bpm_sources" not in tablas:
        conn.execute(
            "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm)"
            " SELECT id, bpm_source, bpm FROM tracks"
            " WHERE bpm IS NOT NULL AND bpm_source IS NOT NULL"
        )
    conn.commit()
    conn.close()


def registrar_bpm_fuente(conn, track_id, fuente, bpm, detalle=None):
    """Records (or updates) what BPM a source reported for a track.
    bpm as None means "I consulted it and it didn't have the track" — this helps
    so enrich_beatport.py doesn't ask for it again.
    Does not commit: the caller is responsible for that."""
    conn.execute(
        "INSERT INTO bpm_sources (track_id, source, bpm, detail) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(track_id, source) DO UPDATE SET bpm = excluded.bpm, detail = excluded.detail",
        (track_id, fuente, bpm, detalle),
    )


if __name__ == "__main__":
    # This lets you run "python db.py" to check that
    # the database is created correctly, without doing anything else.
    init_db()
    print(f"Database ready at: {DB_PATH}")
