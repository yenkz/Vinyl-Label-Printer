"""
db.py — Local database management.

We use SQLite because it comes with Python (no installation needed)
and stores everything in a single file: vinyl_labels.db, created
in this folder the first time you run something.

Think of it as a mini spreadsheet with several "tabs":
  - releases:    one row per vinyl record (LP)
  - tracks:      one row per song on each vinyl
  - bpm_sources: what BPM EACH source reported for each track (Beatport,
                 YouTube measurement, or historical sources). The "chosen" BPM
                 lives in tracks.bpm; the full details are stored here,
                 which the editor shows so you can validate intelligently.
  - key_sources: the same provenance history for musical-key estimates
                 (Beatport, Essentia, librosa, or a manual choice).
  - pending_downloads: downloads that download_music.py queued with slskd
                 and hasn't collected yet — slskd keeps downloading after
                 the script exits, so the next run picks these back up.
  - failed_downloads: tracks download_music.py couldn't find on Soulseek,
                 with a timestamp, so repeat runs skip them for a week
                 instead of re-searching the whole network for nothing.
  - workflow_steps: which automatic steps have already been attempted for
                 each record. This makes normal runs delta-only: records that
                 were already in the collection are not searched again.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl_labels.db"

# Steps that operate on a record after it has been imported. When the workflow
# ledger is introduced to an existing database, all existing records are
# baselined as complete for these steps; only records fetched afterwards are
# considered new. Any step can still ignore the ledger with its --all option.
WORKFLOW_STEPS = ("beatport", "bandcamp", "spotify", "analyze", "render")


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
            key_alt           TEXT,     -- alternative if local key detectors disagreed
            key_needs_review  INTEGER DEFAULT 0,
            key_verified      INTEGER DEFAULT 0,
            key_strength      REAL,     -- primary detector's uncalibrated score
            isrc              TEXT,     -- ISRC code for track (from Spotify or Beatport); not printed
            audio_path        TEXT,     -- local path to digital copy downloaded from Soulseek; NULL = not downloaded
            audio_format      TEXT,     -- "aiff", "flac", "wav", "mp3"
            audio_source      TEXT,     -- Soulseek user the file came from (provenance)
            FOREIGN KEY (release_id) REFERENCES releases(release_id)
        );

        CREATE TABLE IF NOT EXISTS bpm_sources (
            track_id  INTEGER NOT NULL,
            source    TEXT NOT NULL,    -- "beatport", "youtube", "manual", or a historical source
            bpm       REAL,             -- NULL = source was consulted and didn't have the track
            detail    TEXT,             -- where it came from exactly (e.g., measured video title)
            PRIMARY KEY (track_id, source),
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        );

        CREATE TABLE IF NOT EXISTS key_sources (
            track_id  INTEGER NOT NULL,
            source    TEXT NOT NULL,    -- "beatport", "essentia", "librosa", or "manual"
            key       TEXT,             -- canonical musical notation ("Am", "F#")
            strength  REAL,             -- detector-specific, not comparable across detectors
            detail    TEXT,             -- exact audio/search result used for the estimate
            PRIMARY KEY (track_id, source),
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        );

        CREATE TABLE IF NOT EXISTS pending_downloads (
            track_id  INTEGER PRIMARY KEY,  -- one live transfer per track
            username  TEXT NOT NULL,        -- Soulseek user the file was requested from
            filename  TEXT NOT NULL,        -- remote path on their side
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        );

        CREATE TABLE IF NOT EXISTS failed_downloads (
            track_id   INTEGER PRIMARY KEY,  -- track we couldn't find on Soulseek
            reason     TEXT,                 -- why (nothing found, every source failed...)
            failed_at  TEXT NOT NULL,        -- ISO timestamp; entries expire after a week
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        );

        CREATE TABLE IF NOT EXISTS workflow_steps (
            release_id    INTEGER NOT NULL,
            step          TEXT NOT NULL,
            completed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (release_id, step),
            FOREIGN KEY (release_id) REFERENCES releases(release_id)
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
    if "key_alt" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN key_alt TEXT")
    if "key_needs_review" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN key_needs_review INTEGER DEFAULT 0")
    if "key_verified" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN key_verified INTEGER DEFAULT 0")
        conn.execute(
            "UPDATE tracks SET key_verified = 1"
            " WHERE key IS NOT NULL AND key_source IN ('beatport', 'manual')"
        )
    if "key_strength" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN key_strength REAL")
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
    if "key_sources" not in tables:
        conn.execute(
            "INSERT OR IGNORE INTO key_sources (track_id, source, key)"
            " SELECT id, key_source, key FROM tracks"
            " WHERE key IS NOT NULL AND key_source IS NOT NULL"
        )
    # Migration to delta-only behavior: everything that was already in the
    # database is the baseline. A later fetch inserts new releases without
    # workflow rows, so every automatic step sees only that delta.
    if "workflow_steps" not in tables:
        conn.executemany(
            "INSERT OR IGNORE INTO workflow_steps (release_id, step)"
            " SELECT release_id, ? FROM releases",
            ((step,) for step in WORKFLOW_STEPS),
        )
    # Introducing local key analysis is new work even for an otherwise
    # baselined collection. Queue only releases that still lack keys, once,
    # so the next normal `make analyze` fills them without requiring --all.
    if "key_sources" not in tables:
        conn.execute(
            "DELETE FROM workflow_steps WHERE step = 'analyze'"
            " AND release_id IN (SELECT DISTINCT release_id FROM tracks WHERE key IS NULL)"
        )
    # Retire bookkeeping for the removed automatic fallback step. Saved BPM
    # values and their source history deliberately remain untouched.
    conn.execute("DELETE FROM workflow_steps WHERE step = 'bpm'")
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


def record_key_source(conn, track_id, source, key, strength=None, detail=None):
    """Records one key source without choosing it as the track's key.

    Strength is detector-specific and is not a calibrated probability.
    Does not commit: the caller is responsible for that.
    """
    conn.execute(
        "INSERT INTO key_sources (track_id, source, key, strength, detail)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(track_id, source) DO UPDATE SET"
        " key = excluded.key, strength = excluded.strength, detail = excluded.detail",
        (track_id, source, key, strength, detail),
    )


def mark_workflow_step(conn, release_id, step):
    """Marks one automatic step as attempted for a release.

    "Completed" here means the source was consulted, not necessarily that it
    found every field. Normal runs do not repeatedly search old misses; pass
    --all when you deliberately want to try them again.
    """
    conn.execute(
        "INSERT INTO workflow_steps (release_id, step) VALUES (?, ?)"
        " ON CONFLICT(release_id, step) DO UPDATE SET completed_at = CURRENT_TIMESTAMP",
        (release_id, step),
    )


if __name__ == "__main__":
    # This lets you run "python db.py" to check that
    # the database is created correctly, without doing anything else.
    init_db()
    print(f"Database ready at: {DB_PATH}")
