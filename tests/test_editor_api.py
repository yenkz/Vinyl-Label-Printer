import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vinyl_labels import db
from vinyl_labels.commands import bpm_manual, edit_bpm


class EditorApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "collection.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO releases (release_id, artist, title) VALUES (1, 'Artist', 'Album')"
        )
        conn.execute(
            "INSERT INTO tracks (id, release_id, position, sort_order, bpm, bpm_source,"
            " bpm_alt, bpm_needs_review, key, key_source, key_alt, key_needs_review)"
            " VALUES (10, 1, 'A1', 0, 120, 'beatport', 60, 1, 'Am', 'essentia', 'C', 1)"
        )
        conn.execute(
            "INSERT INTO tracks (id, release_id, position, sort_order)"
            " VALUES (20, 1, 'A2', 1)"
        )
        conn.execute(
            "INSERT INTO bpm_sources (track_id, source, bpm) VALUES (10, 'beatport', 120)"
        )
        conn.execute(
            "INSERT INTO key_sources (track_id, source, key, strength)"
            " VALUES (10, 'essentia', 'Am', 0.8)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def request(self, path, payload):
        encoded = json.dumps(payload).encode("utf-8")
        handler = object.__new__(edit_bpm.Handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler.respond = Mock()
        handler.send_error = Mock()
        edit_bpm.Handler.do_POST(handler)
        return handler

    def fetch_track(self, track_id=10):
        conn = db.get_connection()
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        conn.close()
        return row

    def test_bpm_save_is_manual_verified_and_records_provenance(self):
        handler = self.request("/api/bpm", {"id": 10, "bpm": 128.5})

        handler.send_error.assert_not_called()
        handler.respond.assert_called_once()
        track = self.fetch_track()
        self.assertEqual((track["bpm"], track["bpm_source"]), (128.5, "manual"))
        self.assertEqual(
            (track["bpm_alt"], track["bpm_needs_review"], track["bpm_verified"]),
            (None, 0, 1),
        )
        conn = db.get_connection()
        source = conn.execute(
            "SELECT bpm FROM bpm_sources WHERE track_id = 10 AND source = 'manual'"
        ).fetchone()
        conn.close()
        self.assertEqual(source["bpm"], 128.5)

    def test_key_save_normalizes_and_records_manual_choice(self):
        handler = self.request("/api/key", {"id": 10, "key": "8A"})

        handler.send_error.assert_not_called()
        response = json.loads(handler.respond.call_args.args[0])
        self.assertEqual((response["key"], response["camelot"]), ("Am", "8A"))
        track = self.fetch_track()
        self.assertEqual((track["key"], track["key_source"]), ("Am", "manual"))
        self.assertEqual(
            (track["key_alt"], track["key_needs_review"], track["key_verified"]),
            (None, 0, 1),
        )

    def test_confirm_endpoints_clear_alternatives_and_preserve_sources(self):
        bpm_handler = self.request("/api/confirm", {"id": 10})
        key_handler = self.request("/api/key-confirm", {"id": 10})

        bpm_handler.send_error.assert_not_called()
        key_handler.send_error.assert_not_called()
        track = self.fetch_track()
        self.assertEqual((track["bpm"], track["bpm_source"]), (120, "beatport"))
        self.assertEqual((track["bpm_alt"], track["bpm_needs_review"]), (None, 0))
        self.assertEqual((track["key"], track["key_source"]), ("Am", "essentia"))
        self.assertEqual((track["key_alt"], track["key_needs_review"]), (None, 0))
        self.assertEqual((track["bpm_verified"], track["key_verified"]), (1, 1))

    def test_missing_or_empty_tracks_are_not_reported_as_successful(self):
        for path, payload in (
            ("/api/bpm", {"id": 999, "bpm": 128}),
            ("/api/key", {"id": 999, "key": "Am"}),
            ("/api/confirm", {"id": 999}),
            ("/api/key-confirm", {"id": 999}),
            ("/api/confirm", {"id": 20}),
            ("/api/key-confirm", {"id": 20}),
        ):
            with self.subTest(path=path, payload=payload):
                handler = self.request(path, payload)
                handler.send_error.assert_called_once_with(404)
                handler.respond.assert_not_called()

    def test_template_is_loaded_from_the_project_directory(self):
        self.assertEqual(edit_bpm.PAGE, edit_bpm.TEMPLATE_PATH.read_text(encoding="utf-8"))
        self.assertIn("manual: 0, beatport: 1, bandcamp: 2", edit_bpm.PAGE)
        handler = object.__new__(edit_bpm.Handler)
        handler.path = "/"
        handler.respond = Mock()
        edit_bpm.Handler.do_GET(handler)
        handler.respond.assert_called_once_with(edit_bpm.PAGE, "text/html")


class ManualCsvTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "collection.db"
        self.csv_path = Path(self.directory.name) / "pending.csv"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.csv_patch = patch.object(bpm_manual, "CSV_PATH", self.csv_path)
        self.db_patch.start()
        self.csv_patch.start()
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO releases (release_id) VALUES (1)")
        conn.execute(
            "INSERT INTO tracks (id, release_id, position, sort_order)"
            " VALUES (10, 1, 'A1', 0), (20, 1, 'A2', 1)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.csv_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    def test_import_skips_invalid_and_unknown_track_ids(self):
        self.csv_path.write_text(
            "track_id,artist,album,position,track_title,bpm\n"
            "10,A,B,A1,Good,125\n"
            "20,A,B,A2,Invalid,5\n"
            "999,A,B,A3,Unknown,130\n",
            encoding="utf-8",
        )

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bpm_manual.import_csv(), 0)
        conn = db.get_connection()
        tracks = conn.execute("SELECT id, bpm FROM tracks ORDER BY id").fetchall()
        sources = conn.execute("SELECT track_id, bpm FROM bpm_sources").fetchall()
        conn.close()
        self.assertEqual([(row["id"], row["bpm"]) for row in tracks], [(10, 125), (20, None)])
        self.assertEqual([(row["track_id"], row["bpm"]) for row in sources], [(10, 125)])

    def test_main_returns_failure_when_import_file_is_missing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bpm_manual.main(["import"]), 1)


if __name__ == "__main__":
    unittest.main()
