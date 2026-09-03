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
  - track_workflow_steps: per-track progress for limited analyzer batches, so
                 clean misses do not prevent later tracks from being reached.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from .paths import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "vinyl_labels.db"
BUSY_TIMEOUT_MS = 5_000
SCHEMA_VERSION = 6

# Steps that operate on a record after it has been imported. When the workflow
# ledger is introduced to an existing database, all existing records are
# baselined as complete for these steps; only records fetched afterwards are
# considered new. Any step can still ignore the ledger with its --all option.
WORKFLOW_STEPS = ("beatport", "bandcamp", "spotify", "analyze", "render")


def get_connection():
    """Open the database with the safety settings every caller relies on."""
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1_000)
    conn.row_factory = sqlite3.Row  # allows accessing columns by name, e.g., row["title"]
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # WAL lets the editor read while a background enrichment command writes.
    # Some special SQLite databases/filesystems do not support it, so retain
    # SQLite's current journal mode rather than making the connection unusable.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def backup_database(backup_dir=None):
    """Create a consistent timestamped copy of the current database.

    ``backup_dir`` defaults to a ``backups`` directory beside the database. The SQLite backup API
    is used instead of copying the file so this remains safe when WAL is active.
    Returns the path to the newly created backup.
    """
    source_path = Path(DB_PATH)
    if not source_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {source_path}")

    destination_dir = (
        Path(backup_dir) if backup_dir is not None else source_path.parent / "backups"
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    suffix = source_path.suffix or ".db"
    destination = destination_dir / f"{source_path.stem}.backup-{timestamp}{suffix}"

    source = sqlite3.connect(source_path, timeout=BUSY_TIMEOUT_MS / 1_000)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    except Exception:
        target.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
    finally:
        source.close()
    return destination


def _add_safety_indexes(conn):
    """Add non-destructive indexes, tolerating ambiguous legacy positions."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_release_id ON tracks(release_id)")
    # Empty/NULL positions occur in some Discogs data and are not identifiers.
    # Do not make an existing database unusable merely because it has duplicate
    # meaningful positions; a later init will add the index once they are fixed.
    duplicate_position = conn.execute(
        "SELECT 1 FROM tracks"
        " WHERE position IS NOT NULL AND TRIM(position) <> ''"
        " GROUP BY release_id, position HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate_position is None:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_tracks_release_position"
            " ON tracks(release_id, position)"
            " WHERE position IS NOT NULL AND TRIM(position) <> ''"
        )


def _remove_orphans(conn):
    """Remove unusable child rows left by historically disabled FK checks."""
    for table in (
        "bpm_sources",
        "key_sources",
        "pending_downloads",
        "failed_downloads",
        "track_workflow_steps",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE NOT EXISTS ("
            f" SELECT 1 FROM tracks WHERE tracks.id = {table}.track_id)"
        )
    conn.execute(
        "DELETE FROM workflow_steps WHERE NOT EXISTS ("
        " SELECT 1 FROM releases"
        " WHERE releases.release_id = workflow_steps.release_id)"
    )


def _add_integrity_triggers(conn):
    """Reject impossible BPM values without rebuilding legacy tables."""
    trigger_statements = (
        """
        CREATE TRIGGER IF NOT EXISTS validate_tracks_bpm_insert
        BEFORE INSERT ON tracks
        WHEN (NEW.bpm IS NOT NULL AND (
                  TYPEOF(NEW.bpm) NOT IN ('integer', 'real')
                  OR NEW.bpm <= 0 OR NEW.bpm > 400
              ))
          OR (NEW.bpm_alt IS NOT NULL AND (
                  TYPEOF(NEW.bpm_alt) NOT IN ('integer', 'real')
                  OR NEW.bpm_alt <= 0 OR NEW.bpm_alt > 400
              ))
        BEGIN
            SELECT RAISE(ABORT, 'BPM must be greater than 0 and at most 400');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS validate_tracks_bpm_update
        BEFORE UPDATE OF bpm, bpm_alt ON tracks
        WHEN (NEW.bpm IS NOT NULL AND (
                  TYPEOF(NEW.bpm) NOT IN ('integer', 'real')
                  OR NEW.bpm <= 0 OR NEW.bpm > 400
              ))
          OR (NEW.bpm_alt IS NOT NULL AND (
                  TYPEOF(NEW.bpm_alt) NOT IN ('integer', 'real')
                  OR NEW.bpm_alt <= 0 OR NEW.bpm_alt > 400
              ))
        BEGIN
            SELECT RAISE(ABORT, 'BPM must be greater than 0 and at most 400');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS validate_bpm_source_insert
        BEFORE INSERT ON bpm_sources
        WHEN NEW.bpm IS NOT NULL AND (
                 TYPEOF(NEW.bpm) NOT IN ('integer', 'real')
                 OR NEW.bpm <= 0 OR NEW.bpm > 400
             )
        BEGIN
            SELECT RAISE(ABORT, 'BPM must be greater than 0 and at most 400');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS validate_bpm_source_update
        BEFORE UPDATE OF bpm ON bpm_sources
        WHEN NEW.bpm IS NOT NULL AND (
                 TYPEOF(NEW.bpm) NOT IN ('integer', 'real')
                 OR NEW.bpm <= 0 OR NEW.bpm > 400
             )
        BEGIN
            SELECT RAISE(ABORT, 'BPM must be greater than 0 and at most 400');
        END
        """,
    )
    for statement in trigger_statements:
        conn.execute(statement)


def _add_cascade_triggers(conn):
    """Give legacy schemas the same delete behavior as the fresh schema."""
    trigger_statements = (
        """
        CREATE TRIGGER IF NOT EXISTS cascade_track_children
        AFTER DELETE ON tracks
        BEGIN
            DELETE FROM bpm_sources WHERE track_id = OLD.id;
            DELETE FROM key_sources WHERE track_id = OLD.id;
            DELETE FROM pending_downloads WHERE track_id = OLD.id;
            DELETE FROM failed_downloads WHERE track_id = OLD.id;
            DELETE FROM track_workflow_steps WHERE track_id = OLD.id;
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS cascade_release_children
        AFTER DELETE ON releases
        BEGIN
            DELETE FROM bpm_sources WHERE track_id IN (
                SELECT id FROM tracks WHERE release_id = OLD.release_id
            );
            DELETE FROM key_sources WHERE track_id IN (
                SELECT id FROM tracks WHERE release_id = OLD.release_id
            );
            DELETE FROM pending_downloads WHERE track_id IN (
                SELECT id FROM tracks WHERE release_id = OLD.release_id
            );
            DELETE FROM failed_downloads WHERE track_id IN (
                SELECT id FROM tracks WHERE release_id = OLD.release_id
            );
            DELETE FROM track_workflow_steps WHERE track_id IN (
                SELECT id FROM tracks WHERE release_id = OLD.release_id
            );
            DELETE FROM tracks WHERE release_id = OLD.release_id;
            DELETE FROM workflow_steps WHERE release_id = OLD.release_id;
        END
        """,
    )
    for statement in trigger_statements:
        conn.execute(statement)


def _migrate_database(conn):
    """Apply the complete schema inside the caller's transaction."""
    # If the sources table doesn't exist yet, after creating it we
    # populate it with what's already in tracks (see below).
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    previous_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if previous_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {previous_version} is newer than this code "
            f"supports ({SCHEMA_VERSION})"
        )
    # Back up established databases before applying any migration. Brand-new
    # empty SQLite files do not need a backup.
    if tables and previous_version < SCHEMA_VERSION:
        backup_database()
    conn.executescript(
        """
        BEGIN IMMEDIATE;

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

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
            sort_order        INTEGER,  -- stable zero-based order within the release
            FOREIGN KEY (release_id) REFERENCES releases(release_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bpm_sources (
            track_id  INTEGER NOT NULL,
            source    TEXT NOT NULL,    -- "beatport", "youtube", "manual", or a historical source
            bpm       REAL,             -- NULL = source was consulted and didn't have the track
            detail    TEXT,             -- where it came from exactly (e.g., measured video title)
            PRIMARY KEY (track_id, source),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS key_sources (
            track_id  INTEGER NOT NULL,
            source    TEXT NOT NULL,    -- "beatport", "essentia", "librosa", or "manual"
            key       TEXT,             -- canonical musical notation ("Am", "F#")
            strength  REAL,             -- detector-specific, not comparable across detectors
            detail    TEXT,             -- exact audio/search result used for the estimate
            PRIMARY KEY (track_id, source),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS pending_downloads (
            track_id  INTEGER PRIMARY KEY,  -- one live transfer per track
            username  TEXT NOT NULL,        -- Soulseek user the file was requested from
            filename  TEXT NOT NULL,        -- remote path on their side
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS failed_downloads (
            track_id   INTEGER PRIMARY KEY,  -- track we couldn't find on Soulseek
            reason     TEXT,                 -- why (nothing found, every source failed...)
            failed_at  TEXT NOT NULL,        -- ISO timestamp; entries expire after a week
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS workflow_steps (
            release_id    INTEGER NOT NULL,
            step          TEXT NOT NULL,
            completed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (release_id, step),
            FOREIGN KEY (release_id) REFERENCES releases(release_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS track_workflow_steps (
            track_id      INTEGER NOT NULL,
            step          TEXT NOT NULL,
            completed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (track_id, step),
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );
        """
    )
    # Migration: if the database existed before we added these
    # columns, we add them now.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    for column, definition in (
        ("position", "TEXT"),
        ("title", "TEXT"),
        ("duration_display", "TEXT"),
        ("bpm", "REAL"),
        ("bpm_source", "TEXT"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {definition}")
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
    if "sort_order" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN sort_order INTEGER")
    # Preserve existing track IDs while assigning deterministic display order.
    conn.execute(
        "UPDATE tracks AS current SET sort_order = ("
        " SELECT COUNT(*) - 1 FROM tracks AS preceding"
        " WHERE preceding.release_id = current.release_id"
        " AND preceding.id <= current.id"
        ") WHERE sort_order IS NULL"
    )
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
    _remove_orphans(conn)
    _add_safety_indexes(conn)
    _add_integrity_triggers(conn)
    _add_cascade_triggers(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
        (
            (1, "initial_schema"),
            (2, "enrichment_and_workflow_columns"),
            (3, "connection_safety_and_indexes"),
            (4, "stable_track_sort_order"),
            (5, "orphan_cleanup_and_bpm_guards"),
            (6, "per_track_workflow_progress"),
        ),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db():
    """Create or atomically migrate the database, then always close it."""
    conn = get_connection()
    try:
        _migrate_database(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
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


def mark_track_workflow_step(conn, track_id, step):
    """Record a clean per-track attempt so limited batches can advance."""
    conn.execute(
        "INSERT INTO track_workflow_steps (track_id, step) VALUES (?, ?)"
        " ON CONFLICT(track_id, step) DO UPDATE SET completed_at = CURRENT_TIMESTAMP",
        (track_id, step),
    )


if __name__ == "__main__":
    # This lets you run "python -m vinyl_labels.db" to check that
    # the database is created correctly, without doing anything else.
    init_db()
    print(f"Database ready at: {DB_PATH}")
