import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vinyl_labels import db


class DatabaseSafetyTests(unittest.TestCase):
    def test_every_connection_enables_safety_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collection.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                conn = db.get_connection()
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0],
                    db.BUSY_TIMEOUT_MS,
                )
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                conn.close()

    def test_fresh_schema_tracks_versions_and_cascades_children(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collection.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                conn = db.get_connection()
                versions = [
                    row[0]
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    db.SCHEMA_VERSION,
                )
                self.assertEqual(versions, list(range(1, db.SCHEMA_VERSION + 1)))

                conn.execute("INSERT INTO releases (release_id) VALUES (1)")
                conn.execute(
                    "INSERT INTO tracks (release_id, position, sort_order) VALUES (1, 'A1', 0)"
                )
                track_id = conn.execute("SELECT id FROM tracks").fetchone()[0]
                conn.execute(
                    "INSERT INTO bpm_sources (track_id, source, bpm) VALUES (?, 'manual', 120)",
                    (track_id,),
                )
                conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
                self.assertIsNone(conn.execute("SELECT 1 FROM bpm_sources").fetchone())
                conn.close()

    def test_legacy_tracks_receive_deterministic_sort_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE releases (release_id INTEGER PRIMARY KEY);
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER NOT NULL,
                    position TEXT,
                    bpm REAL,
                    bpm_source TEXT
                );
                INSERT INTO releases (release_id) VALUES (1), (2);
                INSERT INTO tracks (id, release_id, position) VALUES
                    (30, 1, 'A2'), (10, 1, 'A1'), (20, 2, 'A1');
                """
            )
            conn.close()

            with patch.object(db, "DB_PATH", path):
                db.init_db()
                migrated = db.get_connection()
                rows = migrated.execute(
                    "SELECT id, sort_order FROM tracks ORDER BY id"
                ).fetchall()
                migrated.close()
                db.init_db()  # An already-current schema must not create another backup.

            self.assertEqual([(row[0], row[1]) for row in rows], [(10, 0), (20, 0), (30, 1)])
            self.assertEqual(
                len(list((Path(directory) / "backups").glob("legacy.backup-*.db"))),
                1,
            )

    def test_migration_removes_orphans_from_every_child_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collection.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()

                # A plain SQLite connection has FK checks off, reproducing rows
                # created before get_connection() enabled them globally.
                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO bpm_sources (track_id, source, bpm) VALUES (99, 'old', 120)"
                )
                conn.execute(
                    "INSERT INTO key_sources (track_id, source, key) VALUES (99, 'old', 'Am')"
                )
                conn.execute(
                    "INSERT INTO workflow_steps (release_id, step) VALUES (99, 'old')"
                )
                conn.commit()
                conn.close()

                db.init_db()
                migrated = db.get_connection()
                for table in (
                    "bpm_sources",
                    "key_sources",
                    "workflow_steps",
                ):
                    self.assertEqual(
                        migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )
                self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
                migrated.close()

    def test_retired_library_migration_preserves_labels_and_is_atomic(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "collection.db"
                with patch.object(db, "DB_PATH", path):
                    db.init_db()
                    conn = db.get_connection()
                    conn.executescript(
                        """
                        PRAGMA user_version = 6;
                        DELETE FROM schema_migrations WHERE version = 7;
                        ALTER TABLE tracks ADD COLUMN audio_path TEXT;
                        ALTER TABLE tracks ADD COLUMN audio_format TEXT;
                        ALTER TABLE tracks ADD COLUMN audio_source TEXT;
                        CREATE TABLE pending_downloads (
                            track_id INTEGER PRIMARY KEY REFERENCES tracks(id),
                            username TEXT, filename TEXT
                        );
                        CREATE TABLE failed_downloads (
                            track_id INTEGER PRIMARY KEY REFERENCES tracks(id),
                            reason TEXT, failed_at TEXT
                        );
                        INSERT INTO releases (release_id, title) VALUES (1, 'Record');
                        INSERT INTO tracks
                            (id, release_id, position, title, bpm, key, isrc, audio_path)
                            VALUES (10, 1, 'A1', 'Song', 128, 'Am', 'ISRC', '/old/file');
                        INSERT INTO bpm_sources VALUES (10, 'manual', 128, 'Kept');
                        INSERT INTO key_sources VALUES (10, 'manual', 'Am', NULL, 'Kept');
                        INSERT INTO pending_downloads VALUES (10, 'peer', 'file');
                        INSERT INTO failed_downloads VALUES (10, 'miss', '2020-01-01');
                        DROP TRIGGER cascade_track_children;
                        CREATE TRIGGER cascade_track_children AFTER DELETE ON tracks
                        BEGIN
                            DELETE FROM pending_downloads WHERE track_id = OLD.id;
                            DELETE FROM failed_downloads WHERE track_id = OLD.id;
                            DELETE FROM bpm_sources WHERE track_id = OLD.id;
                            DELETE FROM key_sources WHERE track_id = OLD.id;
                        END;
                        """
                    )
                    # Reproduce both old triggers referring to the retired tables.
                    sql = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name = 'cascade_release_children'"
                    ).fetchone()[0]
                    conn.execute("DROP TRIGGER cascade_release_children")
                    conn.execute(sql.replace(
                        "BEGIN", "BEGIN DELETE FROM pending_downloads WHERE track_id IN "
                        "(SELECT id FROM tracks WHERE release_id = OLD.release_id);", 1
                    ))
                    conn.commit()
                    conn.close()
                    if fail:
                        with patch.object(db, "_add_integrity_triggers", side_effect=RuntimeError):
                            with self.assertRaises(RuntimeError):
                                db.init_db()
                    else:
                        db.init_db()
                        db.init_db()  # Repeated initialization is safe and creates no extra backup.
                    conn = db.get_connection()
                    row = conn.execute(
                        "SELECT id, title, bpm, key, isrc FROM tracks"
                    ).fetchone()
                    self.assertEqual(tuple(row), (10, 'Song', 128, 'Am', 'ISRC'))
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
                    tables = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    self.assertEqual("audio_path" in columns, fail)
                    self.assertEqual("pending_downloads" in tables, fail)
                    self.assertEqual("failed_downloads" in tables, fail)
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                                     6 if fail else db.SCHEMA_VERSION)
                    if not fail:
                        self.assertFalse({"audio_format", "audio_source"} & columns)
                        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        conn.execute("DELETE FROM releases WHERE release_id = 1")
                        for table in ("tracks", "bpm_sources", "key_sources"):
                            self.assertIsNone(conn.execute(f"SELECT 1 FROM {table}").fetchone())
                    conn.close()
                    backups = list((Path(directory) / "backups").glob("*.db"))
                    self.assertEqual(len(backups), 1)
                    copied = sqlite3.connect(backups[0])
                    self.assertEqual(copied.execute("SELECT audio_path FROM tracks").fetchone()[0],
                                     "/old/file")
                    copied.close()

    def test_bpm_guards_allow_null_and_reject_impossible_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collection.db"
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                conn = db.get_connection()
                conn.execute("INSERT INTO releases (release_id) VALUES (1)")
                conn.execute("INSERT INTO tracks (release_id, bpm) VALUES (1, NULL)")
                track_id = conn.execute("SELECT id FROM tracks").fetchone()[0]
                with self.assertRaisesRegex(sqlite3.IntegrityError, "BPM must"):
                    conn.execute("UPDATE tracks SET bpm = 0 WHERE id = ?", (track_id,))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "BPM must"):
                    conn.execute(
                        "INSERT INTO bpm_sources (track_id, source, bpm)"
                        " VALUES (?, 'bad', 401)",
                        (track_id,),
                    )
                conn.execute("UPDATE tracks SET bpm = 128.5 WHERE id = ?", (track_id,))
                conn.close()

    def test_unique_position_index_is_added_only_when_legacy_data_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE releases (release_id INTEGER PRIMARY KEY);
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER NOT NULL,
                    position TEXT,
                    bpm REAL,
                    bpm_source TEXT
                );
                INSERT INTO releases (release_id) VALUES (1);
                INSERT INTO tracks (id, release_id, position) VALUES
                    (10, 1, 'A1'), (20, 1, 'A1');
                """
            )
            conn.close()

            with patch.object(db, "DB_PATH", path):
                db.init_db()
                migrated = db.get_connection()
                self.assertIsNone(
                    migrated.execute(
                        "SELECT 1 FROM sqlite_master"
                        " WHERE type = 'index' AND name = 'uq_tracks_release_position'"
                    ).fetchone()
                )
                migrated.execute("DELETE FROM tracks WHERE id = 20")
                migrated.commit()
                migrated.close()

                # Index creation is retried even though the schema version is current.
                db.init_db()
                migrated = db.get_connection()
                self.assertIsNotNone(
                    migrated.execute(
                        "SELECT 1 FROM sqlite_master"
                        " WHERE type = 'index' AND name = 'uq_tracks_release_position'"
                    ).fetchone()
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    migrated.execute(
                        "INSERT INTO tracks (release_id, position) VALUES (1, 'A1')"
                    )
                migrated.close()

    def test_backup_database_uses_a_consistent_sqlite_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collection.db"
            backup_dir = Path(directory) / "backups"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE example (value TEXT)")
            conn.execute("INSERT INTO example VALUES ('kept')")
            conn.commit()
            conn.close()

            with patch.object(db, "DB_PATH", path):
                backup_path = db.backup_database(backup_dir)

            self.assertEqual(backup_path.parent, backup_dir)
            self.assertTrue(backup_path.name.startswith("collection.backup-"))
            copied = sqlite3.connect(backup_path)
            self.assertEqual(copied.execute("SELECT value FROM example").fetchone()[0], "kept")
            copied.close()

    def test_legacy_schema_gets_equivalent_cascade_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE releases (release_id INTEGER PRIMARY KEY);
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER NOT NULL,
                    position TEXT,
                    bpm REAL,
                    bpm_source TEXT,
                    FOREIGN KEY (release_id) REFERENCES releases(release_id)
                );
                """
            )
            conn.close()

            with patch.object(db, "DB_PATH", path):
                db.init_db()
                migrated = db.get_connection()
                migrated.execute("INSERT INTO releases (release_id) VALUES (1)")
                migrated.execute(
                    "INSERT INTO tracks (id, release_id, position) VALUES (10, 1, 'A1')"
                )
                migrated.execute(
                    "INSERT INTO bpm_sources (track_id, source, bpm) "
                    "VALUES (10, 'manual', 128)"
                )
                migrated.execute(
                    "INSERT INTO key_sources (track_id, source, key) "
                    "VALUES (10, 'manual', 'Am')"
                )
                migrated.execute(
                    "INSERT INTO workflow_steps (release_id, step) VALUES (1, 'render')"
                )
                migrated.execute("DELETE FROM releases WHERE release_id = 1")
                for table in ("tracks", "bpm_sources", "key_sources", "workflow_steps"):
                    self.assertEqual(
                        migrated.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )
                migrated.close()

    def test_failed_migration_rolls_back_and_closes_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE releases (release_id INTEGER PRIMARY KEY);
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER NOT NULL,
                    position TEXT,
                    bpm REAL,
                    bpm_source TEXT
                );
                """
            )
            conn.close()

            with (
                patch.object(db, "DB_PATH", path),
                patch.object(
                    db,
                    "_add_integrity_triggers",
                    side_effect=RuntimeError("induced migration failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "induced migration failure"):
                    db.init_db()

            reopened = sqlite3.connect(path)
            columns = {
                row[1] for row in reopened.execute("PRAGMA table_info(tracks)")
            }
            self.assertNotIn("sort_order", columns)
            self.assertEqual(reopened.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(
                reopened.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
            )
            reopened.close()


if __name__ == "__main__":
    unittest.main()
