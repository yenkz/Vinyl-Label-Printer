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
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS releases (
            release_id   INTEGER PRIMARY KEY,   -- Discogs record ID
            artist       TEXT,
            title        TEXT,
            year         INTEGER,
            label        TEXT,     -- record label, from Discogs
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
            audio_path        TEXT,     -- local path to digital copy downloaded from Soulseek; NULL = not downloaded
            audio_format      TEXT,     -- "aiff", "flac", "wav", "mp3"
            audio_source      TEXT,     -- Soulseek user the file came from (provenance)
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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "artist" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN artist TEXT")
    if "bpm_alt" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_alt REAL")
    if "bpm_needs_review" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_needs_review INTEGER DEFAULT 0")
    if "bpm_verified" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN bpm_verified INTEGER DEFAULT 0")
        # BPM entered manually were already validated by you when you wrote them.
        conn.execute("UPDATE tracks SET bpm_verified = 1 WHERE bpm_source = 'manual'")
    if "isrc" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN isrc TEXT")
    if "key" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN key TEXT")
    if "key_source" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN key_source TEXT")
    # Digital copy downloaded from Soulseek (download_music.py):
    if "audio_path" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN audio_path TEXT")     # local file, NULL = not downloaded
    if "audio_format" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN audio_format TEXT")   # "aiff", "flac", "wav", "mp3"
    if "audio_source" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN audio_source TEXT")   # Soulseek user it came from
    columns_releases = {row["name"] for row in conn.execute("PRAGMA table_info(releases)")}
    # Migration: the record-label column was renamed from "sello" to "label".
    if "sello" in columns_releases and "label" not in columns_releases:
        conn.execute("ALTER TABLE releases RENAME COLUMN sello TO label")
        columns_releases.add("label")
    for column in ("label", "catno", "released", "cover_path"):
        if column not in columns_releases:
            conn.execute(f"ALTER TABLE releases ADD COLUMN {column} TEXT")
    # Migration: the first time bpm_sources appears, we record there
    # the BPM that already existed in tracks, each under its source.
    if "bpm_sources" not in tables:
        conn.execute(
            "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm)"
            " SELECT id, bpm_source, bpm FROM tracks"
            " WHERE bpm IS NOT NULL AND bpm_source IS NOT NULL"
        )
    conn.commit()
    conn.close()


def record_bpm_source(conn, track_id, source, bpm, detail=None):
    """Records (or updates) what BPM a source reported for a track.
    bpm as None means "I consulted it and it didn't have the track" — this helps
    so enrich_beatport.py doesn't ask for it again.
    Does not commit: the caller is responsible for that."""
    conn.execute(
        "INSERT INTO bpm_sources (track_id, source, bpm, detail) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(track_id, source) DO UPDATE SET bpm = excluded.bpm, detail = excluded.detail",
        (track_id, source, bpm, detail),
    )


if __name__ == "__main__":
    # This lets you run "python db.py" to check that
    # the database is created correctly, without doing anything else.
    init_db()
    print(f"Database ready at: {DB_PATH}")
