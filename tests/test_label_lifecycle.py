import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vinyl_labels.commands import print_labels, render_labels
from vinyl_labels.commands.render_labels import (
    archive_orphaned_pending,
    archive_pending_artifacts,
    release_id_from_path,
    unique_artifact_path,
)


class LabelArtifactLifecycleTests(unittest.TestCase):
    def test_release_id_parser_requires_positive_id_at_filename_end(self):
        self.assertEqual(release_id_from_path("Artist - Title (123).png"), 123)
        self.assertEqual(release_id_from_path("Artist - Title (123).PNG"), 123)
        self.assertIsNone(release_id_from_path("Artist (0).png"))
        self.assertIsNone(release_id_from_path("Artist (123).png.backup"))
        self.assertIsNone(release_id_from_path("Artist 123.png"))

    def test_archiving_superseded_pending_keeps_canonical_and_print_history(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            printed = output / "printed"
            printed.mkdir()
            canonical = output / "New Artist - New Title (42).png"
            old_pending = output / "Old Artist - Old Title (42).png"
            old_printed = printed / "Old Artist - Old Title (42).png"
            unrelated = output / "Other (7).png"
            for path, contents in (
                (canonical, b"new"),
                (old_pending, b"old-pending"),
                (old_printed, b"old-printed"),
                (unrelated, b"other"),
            ):
                path.write_bytes(contents)

            archived = archive_pending_artifacts(output, 42, keep=canonical)

            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), b"old-pending")
            self.assertFalse(old_pending.exists())
            self.assertEqual(canonical.read_bytes(), b"new")
            self.assertEqual(old_printed.read_bytes(), b"old-printed")
            self.assertEqual(unrelated.read_bytes(), b"other")

    def test_orphan_cleanup_only_archives_identifiable_non_collection_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            current = output / "Current (1).png"
            orphan = output / "Removed (2).png"
            manual = output / "notes.png"
            current.write_bytes(b"current")
            orphan.write_bytes(b"orphan")
            manual.write_bytes(b"manual")

            archived = archive_orphaned_pending(output, {1})

            self.assertEqual([path.read_bytes() for path in archived], [b"orphan"])
            self.assertTrue(current.exists())
            self.assertTrue(manual.exists())
            self.assertFalse(orphan.exists())

    def test_unique_history_name_preserves_release_id_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory)
            original = history / "Artist - Title (81).png"
            original.write_bytes(b"first")

            destination = unique_artifact_path(history, original.name)

            self.assertEqual(destination.name, "Artist - Title [revision 2] (81).png")
            self.assertEqual(release_id_from_path(destination), 81)

    def test_move_to_printed_never_overwrites_existing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "Artist - Title (9).png"
            printed = root / "printed"
            printed.mkdir()
            existing = printed / pending.name
            pending.write_bytes(b"new revision")
            existing.write_bytes(b"old revision")

            destination = print_labels.move_to_printed(pending, printed)

            self.assertEqual(existing.read_bytes(), b"old revision")
            self.assertEqual(destination.read_bytes(), b"new revision")
            self.assertEqual(destination.name, "Artist - Title [revision 2] (9).png")
            self.assertFalse(pending.exists())

    def test_render_archives_stale_pending_when_print_history_is_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "labels"
            printed = output / "printed"
            printed.mkdir(parents=True)
            database = root / "library.db"
            conn = sqlite3.connect(database)
            conn.executescript(
                """
                CREATE TABLE releases (
                    release_id INTEGER PRIMARY KEY,
                    artist TEXT,
                    title TEXT,
                    label TEXT,
                    catno TEXT,
                    released TEXT,
                    year INTEGER,
                    cover_path TEXT
                );
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER,
                    position TEXT,
                    sort_order INTEGER,
                    bpm REAL,
                    bpm_verified INTEGER,
                    key TEXT
                );
                INSERT INTO releases (release_id, artist, title)
                VALUES (42, 'Artist', 'Title');
                INSERT INTO tracks
                    (id, release_id, position, sort_order, bpm, bpm_verified)
                VALUES (1, 42, 'A1', 0, 128, 1);
                """
            )
            conn.close()

            canonical = output / "Artist - Title (42).png"
            current = printed / canonical.name
            stale_image = render_labels.Image.new("RGB", (8, 8), "black")
            current_image = render_labels.Image.new("RGB", (8, 8), "white")
            stale_image.save(canonical)
            current_image.save(current)

            def connect():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            with (
                mock.patch.object(render_labels, "OUTPUT_DIR", output),
                mock.patch.object(render_labels, "init_db"),
                mock.patch.object(render_labels, "get_connection", side_effect=connect),
                mock.patch.object(render_labels, "load_fonts", return_value=(None,) * 4),
                mock.patch.object(
                    render_labels,
                    "render_release",
                    return_value=current_image,
                ),
                mock.patch.object(render_labels, "mark_workflow_step"),
            ):
                status = render_labels.main([])

            self.assertEqual(status, 0)
            self.assertFalse(canonical.exists())
            self.assertTrue(current.exists())
            archived = list((output / "obsolete").glob("*.png"))
            self.assertEqual(len(archived), 1)
            with render_labels.Image.open(archived[0]) as image:
                self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))


class PrintableValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE releases (
                release_id INTEGER PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL
            );
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                release_id INTEGER NOT NULL,
                bpm REAL,
                bpm_verified INTEGER NOT NULL
            );
            INSERT INTO releases (release_id, artist, title)
            VALUES (10, 'Current Artist', 'Current Title'),
                   (20, 'Other Artist', 'Other Title');
            INSERT INTO tracks (release_id, bpm, bpm_verified)
            VALUES (10, 125, 1), (20, 130, 1);
            """
        )

    def tearDown(self):
        self.conn.close()

    def validate(self, paths):
        class NonClosingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, *args, **kwargs):
                return self.connection.execute(*args, **kwargs)

            def close(self):
                pass

        with (
            mock.patch.object(print_labels, "init_db"),
            mock.patch.object(
                print_labels,
                "get_connection",
                return_value=NonClosingConnection(self.conn),
            ),
        ):
            return print_labels.validated_images(paths)

    def test_only_current_canonical_filename_is_printable(self):
        canonical = Path("Current Artist - Current Title (10).png")
        stale = Path("Old Artist - Old Title (20).png")

        printable, blocked = self.validate([canonical, stale])

        self.assertEqual(printable, [canonical])
        self.assertEqual(blocked, [stale])

    def test_duplicate_release_ids_are_all_blocked(self):
        first = Path("Current Artist - Current Title (10).png")
        duplicate = Path("Some Copy (10).png")

        printable, blocked = self.validate([first, duplicate])

        self.assertEqual(printable, [])
        self.assertCountEqual(blocked, [first, duplicate])


if __name__ == "__main__":
    unittest.main()
