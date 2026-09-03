import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vinyl_labels import db
from vinyl_labels.commands import fetch_discogs


def artist(name):
    return SimpleNamespace(name=name)


def track(position, title, duration="3:00", artists=()):
    return SimpleNamespace(
        position=position,
        title=title,
        duration=duration,
        artists=list(artists),
    )


def release(release_id, title, tracks, *, cover=False):
    return SimpleNamespace(
        id=release_id,
        title=title,
        year=2026,
        artists=[artist("Artist")],
        labels=[],
        tracklist=tracks,
        data={"released": "2026-01-02", "images": ([{"type": "primary", "uri": "cover"}] if cover else [])},
        refresh=lambda: None,
    )


class FetchRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "test.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def run_fetch(
        self,
        releases,
        arguments=("--all",),
        cover_side_effect=None,
        reported_count=None,
    ):
        folder = SimpleNamespace(
            count=len(releases) if reported_count is None else reported_count,
            releases=[SimpleNamespace(id=r.id, release=r) for r in releases],
        )
        identity = SimpleNamespace(username="tester", collection_folders=[folder])
        client = SimpleNamespace(identity=lambda: identity)
        with (
            patch.object(fetch_discogs.config, "DISCOGS_USER_TOKEN", "token"),
            patch.object(fetch_discogs.discogs_client, "Client", return_value=client),
            patch.object(fetch_discogs.time, "sleep"),
            patch.object(fetch_discogs, "download_cover", side_effect=cover_side_effect),
        ):
            return fetch_discogs.main(list(arguments))

    def seed_release(self):
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO releases (release_id, artist, title) VALUES (1, 'Artist', 'Old')"
        )
        conn.execute(
            "INSERT INTO tracks"
            " (id, release_id, position, title, duration_display, bpm, bpm_source, sort_order)"
            " VALUES (10, 1, 'A1', 'Song One', '3:00', 128, 'manual', 0),"
            "        (11, 1, 'B1', 'Old Song', '4:00', 130, 'manual', 1)"
        )
        conn.execute(
            "INSERT INTO bpm_sources (track_id, source, bpm) VALUES (10, 'manual', 128), (11, 'manual', 130)"
        )
        conn.commit()
        conn.close()

    def test_refresh_retains_same_track_id_but_resets_replaced_position(self):
        self.seed_release()

        status = self.run_fetch(
            [release(1, "Refreshed", [track("A1", "Song One!", ""), track("B1", "New Song")])]
        )

        self.assertEqual(status, 0)
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, position, title, duration_display, bpm FROM tracks"
            " WHERE release_id = 1 ORDER BY sort_order"
        ).fetchall()
        self.assertEqual((rows[0]["id"], rows[0]["bpm"], rows[0]["duration_display"]), (10, 128, "3:00"))
        self.assertEqual((rows[1]["title"], rows[1]["bpm"]), ("New Song", None))
        self.assertNotEqual(rows[1]["id"], 11)
        self.assertIsNone(
            conn.execute("SELECT 1 FROM bpm_sources WHERE track_id = 11").fetchone()
        )
        conn.close()

    def test_failed_release_is_rolled_back_before_later_release_commits(self):
        self.seed_release()
        first = release(1, "Should Roll Back", [track("A1", "Changed")], cover=True)
        second = release(2, "Good", [track("A1", "Good Track")])

        def download(_url, release_id):
            if release_id == 1:
                raise RuntimeError("broken cover")
            return None

        status = self.run_fetch([first, second], cover_side_effect=download)

        self.assertEqual(status, 1)
        conn = db.get_connection()
        original = conn.execute(
            "SELECT title FROM releases WHERE release_id = 1"
        ).fetchone()
        original_tracks = conn.execute(
            "SELECT id, title, bpm FROM tracks WHERE release_id = 1 ORDER BY sort_order"
        ).fetchall()
        imported = conn.execute(
            "SELECT title FROM releases WHERE release_id = 2"
        ).fetchone()
        conn.close()
        self.assertEqual(original["title"], "Old")
        self.assertEqual(
            [(row["id"], row["title"], row["bpm"]) for row in original_tracks],
            [(10, "Song One", 128), (11, "Old Song", 130)],
        )
        self.assertEqual(imported["title"], "Good")

    def test_truncated_collection_listing_never_removes_local_releases(self):
        self.seed_release()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO releases (release_id, artist, title) "
            "VALUES (2, 'Artist', 'Must Stay')"
        )
        conn.execute(
            "INSERT INTO tracks "
            "(release_id, position, title, duration_display, sort_order) "
            "VALUES (2, 'A1', 'Saved Track', '3:00', 0)"
        )
        conn.commit()
        conn.close()

        status = self.run_fetch(
            [release(1, "Refreshed", [track("A1", "Song One")])],
            reported_count=2,
        )

        self.assertEqual(status, 1)
        conn = db.get_connection()
        remaining = [
            row[0]
            for row in conn.execute("SELECT release_id FROM releases ORDER BY release_id")
        ]
        conn.close()
        self.assertEqual(remaining, [1, 2])

    def test_empty_refresh_tracklist_keeps_existing_tracks(self):
        self.seed_release()

        status = self.run_fetch([release(1, "Empty Response", [])])

        self.assertEqual(status, 1)
        conn = db.get_connection()
        saved_release = conn.execute(
            "SELECT title FROM releases WHERE release_id = 1"
        ).fetchone()[0]
        saved_tracks = [
            row[0]
            for row in conn.execute(
                "SELECT title FROM tracks WHERE release_id = 1 ORDER BY sort_order"
            )
        ]
        conn.close()
        self.assertEqual(saved_release, "Old")
        self.assertEqual(saved_tracks, ["Song One", "Old Song"])


if __name__ == "__main__":
    unittest.main()
