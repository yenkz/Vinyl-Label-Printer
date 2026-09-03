import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from vinyl_labels.commands import analyze_bpm, audit_bpm


class FakeYoutubeDL:
    entries = []

    def __init__(self, _options):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _query, download=False):
        if download:
            raise AssertionError("tests must not download audio")
        return {"entries": self.entries}


class FailingYoutubeDL(FakeYoutubeDL):
    def extract_info(self, _query, download=False):
        raise OSError("provider unavailable")


class AnalyzeTrackProvenanceTests(unittest.TestCase):
    def analyze_platform(self, platform):
        display = {
            "bandcamp": "Bandcamp",
            "youtube": "YouTube",
            "soundcloud": "SoundCloud",
        }[platform]
        video = {
            "title": "Artist - Track",
            "url": f"https://{platform}.example/track",
            "duration": 300,
        }
        measured = analyze_bpm.AudioAnalysis(bpm=128)

        if platform == "bandcamp":
            search_patch = patch.object(
                analyze_bpm, "search_bandcamp", return_value=([video], [])
            )
            searchers_patch = patch.object(analyze_bpm, "SEARCHERS", [])
            ytdlp_patch = patch.object(analyze_bpm, "YoutubeDL", FakeYoutubeDL)
            select_patch = patch.object(analyze_bpm, "select_videos")
        else:
            FakeYoutubeDL.entries = [video]
            search_patch = patch.object(
                analyze_bpm, "search_bandcamp", return_value=([], [])
            )
            searchers_patch = patch.object(
                analyze_bpm, "SEARCHERS", [(display, f"{platform}search6")]
            )
            ytdlp_patch = patch.object(analyze_bpm, "YoutubeDL", FakeYoutubeDL)
            select_patch = patch.object(
                analyze_bpm, "select_videos", return_value=([video], [])
            )

        with (
            search_patch,
            searchers_patch,
            ytdlp_patch,
            select_patch,
            patch.object(
                analyze_bpm, "download_and_measure", return_value=measured
            ),
        ):
            result, source = analyze_bpm.analyze_track(
                "Artist", "Track", 300, "/unused", need_key=False
            )

        self.assertEqual(result.bpm, 128)
        self.assertEqual(source.platform, platform)
        self.assertEqual(source.title, video["title"])
        self.assertEqual(source.url, video["url"])
        self.assertEqual(source.analysis_version, analyze_bpm.LOCAL_ANALYSIS_VERSION)
        self.assertIn(display, source.detail)
        self.assertIn(f"analysis={analyze_bpm.LOCAL_ANALYSIS_VERSION}", source.detail)
        self.assertIn(video["url"], source.detail)

    def test_bandcamp_source_is_structured(self):
        self.analyze_platform("bandcamp")

    def test_youtube_source_is_structured(self):
        self.analyze_platform("youtube")

    def test_soundcloud_source_is_structured(self):
        self.analyze_platform("soundcloud")

    def test_duration_rescue_retains_platform_and_marks_result_doubtful(self):
        video = {
            "title": "Artist - Track (long version)",
            "url": "https://bandcamp.example/long-version",
            "duration": 420,
        }
        with (
            patch.object(
                analyze_bpm, "search_bandcamp", return_value=([], [video])
            ),
            patch.object(analyze_bpm, "SEARCHERS", []),
            patch.object(
                analyze_bpm,
                "download_and_measure",
                return_value=analyze_bpm.AudioAnalysis(bpm=128),
            ),
        ):
            result, source = analyze_bpm.analyze_track(
                "Artist", "Track", 300, "/unused", need_key=False
            )

        self.assertTrue(result.bpm_doubtful)
        self.assertEqual(source.platform, "bandcamp")
        self.assertIn("different edition?", source.detail)

    def test_successful_empty_search_across_providers_is_not_retryable(self):
        FakeYoutubeDL.entries = []
        with (
            patch.object(
                analyze_bpm, "search_bandcamp", return_value=([], [])
            ),
            patch.object(analyze_bpm, "YoutubeDL", FakeYoutubeDL),
        ):
            result, source = analyze_bpm.analyze_track(
                "Artist", "Track", 300, "/unused", need_key=False
            )

        self.assertIsNone(result.bpm)
        self.assertFalse(source.retryable)

    def test_provider_failures_return_structured_retryable_source(self):
        with (
            patch.object(
                analyze_bpm,
                "search_bandcamp",
                side_effect=analyze_bpm.AudioProviderError("search unavailable"),
            ),
            patch.object(analyze_bpm, "YoutubeDL", FailingYoutubeDL),
        ):
            result, source = analyze_bpm.analyze_track(
                "Artist", "Track", 300, "/unused", need_key=False
            )

        self.assertIsNone(result.bpm)
        self.assertTrue(source.retryable)
        self.assertIn("Bandcamp", source.detail)
        self.assertIn("YouTube", source.detail)
        self.assertIn("SoundCloud", source.detail)

    def test_bandcamp_query_and_page_failures_are_retryable(self):
        with patch.object(
            analyze_bpm.requests,
            "post",
            side_effect=analyze_bpm.requests.ConnectionError("offline"),
        ):
            with self.assertRaises(analyze_bpm.AudioProviderError):
                analyze_bpm.search_bandcamp("Artist", "Track", 300)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "auto": {
                "results": [
                    {
                        "type": "t",
                        "item_url_path": "https://artist.bandcamp.com/track/track",
                        "name": "Track",
                        "band_name": "Artist",
                    }
                ]
            }
        }
        with (
            patch.object(analyze_bpm.requests, "post", return_value=response),
            patch.object(analyze_bpm, "YoutubeDL", FailingYoutubeDL),
        ):
            with self.assertRaises(analyze_bpm.AudioProviderError):
                analyze_bpm.search_bandcamp("Artist", "Track", 300)


def create_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE releases (
            release_id INTEGER PRIMARY KEY,
            artist TEXT,
            title TEXT,
            catno TEXT
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            release_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            title TEXT,
            artist TEXT,
            duration_display TEXT,
            bpm REAL,
            bpm_source TEXT,
            bpm_alt REAL,
            bpm_needs_review INTEGER NOT NULL DEFAULT 0,
            bpm_verified INTEGER NOT NULL DEFAULT 0,
            key TEXT
        );
        CREATE TABLE bpm_sources (
            track_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            bpm REAL,
            detail TEXT,
            UNIQUE(track_id, source)
        );
        CREATE TABLE workflow_steps (
            release_id INTEGER NOT NULL,
            step TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(release_id, step)
        );
        CREATE TABLE track_workflow_steps (
            track_id INTEGER NOT NULL,
            step TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(track_id, step)
        );
        """
    )
    conn.close()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class PersistenceTests(unittest.TestCase):
    def test_analyzer_persists_the_selected_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.execute(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, key) "
                "VALUES (10, 1, 1, 'Track', '5:00', 'Am')"
            )
            conn.commit()
            conn.close()

            source = analyze_bpm.AudioSource(
                platform="bandcamp",
                title="Artist - Track",
                url="https://bandcamp.example/track",
            )
            with (
                patch.object(
                    analyze_bpm,
                    "parse_arguments",
                    return_value=SimpleNamespace(all=False, limit=None, pace=0),
                ),
                patch.object(analyze_bpm, "init_db"),
                patch.object(analyze_bpm, "get_connection", side_effect=lambda: connect(path)),
                patch.object(analyze_bpm, "mark_workflow_step"),
                patch.object(
                    analyze_bpm,
                    "analyze_track",
                    return_value=(analyze_bpm.AudioAnalysis(bpm=128), source),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                analyze_bpm.main()

            conn = connect(path)
            track = conn.execute(
                "SELECT bpm, bpm_source FROM tracks WHERE id = 10"
            ).fetchone()
            recorded = conn.execute(
                "SELECT source, detail FROM bpm_sources WHERE track_id = 10"
            ).fetchone()
            conn.close()

        self.assertEqual((track["bpm"], track["bpm_source"]), (128, "bandcamp"))
        self.assertEqual(recorded["source"], "bandcamp")
        self.assertIn("analysis=local-audio-v1", recorded["detail"])

    def test_audit_accepts_legacy_youtube_and_skips_modern_local_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.executemany(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, bpm, "
                " bpm_source, bpm_needs_review, bpm_verified, key) "
                "VALUES (?, 1, ?, ?, '5:00', 128, ?, 0, 0, 'Am')",
                [
                    (10, 1, "Legacy", "youtube"),
                    (20, 2, "Modern", "bandcamp"),
                ],
            )
            conn.executemany(
                "INSERT INTO bpm_sources (track_id, source, bpm, detail) "
                "VALUES (?, ?, 128, ?)",
                [
                    (10, "youtube", None),
                    (20, "bandcamp", "Modern [Bandcamp; analysis=local-audio-v1]"),
                ],
            )
            conn.commit()
            conn.close()

            source = analyze_bpm.AudioSource(
                platform="soundcloud",
                title="Artist - Legacy",
                url="https://soundcloud.example/legacy",
            )
            with (
                patch.object(audit_bpm, "init_db"),
                patch.object(audit_bpm, "get_connection", side_effect=lambda: connect(path)),
                patch.object(
                    audit_bpm,
                    "analyze_track",
                    return_value=(analyze_bpm.AudioAnalysis(bpm=128), source),
                ) as analyze,
                patch.object(audit_bpm.time, "sleep"),
                patch.object(sys, "argv", ["audit_bpm.py"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                audit_bpm.main()

            conn = connect(path)
            legacy = conn.execute(
                "SELECT bpm_source FROM tracks WHERE id = 10"
            ).fetchone()
            modern = conn.execute(
                "SELECT bpm_source FROM tracks WHERE id = 20"
            ).fetchone()
            recorded = conn.execute(
                "SELECT detail FROM bpm_sources "
                "WHERE track_id = 10 AND source = 'soundcloud'"
            ).fetchone()
            conn.close()

        analyze.assert_called_once()
        self.assertEqual(legacy["bpm_source"], "soundcloud")
        self.assertEqual(modern["bpm_source"], "bandcamp")
        self.assertIn("analysis=local-audio-v1", recorded["detail"])

    def test_audit_returns_failure_for_retryable_provider_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.execute(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, bpm, "
                " bpm_source, bpm_needs_review, bpm_verified, key) "
                "VALUES (10, 1, 1, 'Legacy', '5:00', 128, 'youtube', 0, 0, 'Am')"
            )
            conn.commit()
            conn.close()

            retryable = analyze_bpm.AudioSource(
                note="all providers unavailable",
                analysis_version=None,
                retryable=True,
            )
            with (
                patch.object(
                    audit_bpm,
                    "parse_arguments",
                    return_value=SimpleNamespace(limit=None),
                ),
                patch.object(audit_bpm, "init_db"),
                patch.object(
                    audit_bpm,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(
                    audit_bpm,
                    "analyze_track",
                    return_value=(analyze_bpm.AudioAnalysis(), retryable),
                ),
                patch.object(audit_bpm.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = audit_bpm.main()

            conn = connect(path)
            track = conn.execute(
                "SELECT bpm, bpm_source FROM tracks WHERE id = 10"
            ).fetchone()
            source_count = conn.execute(
                "SELECT COUNT(*) FROM bpm_sources WHERE track_id = 10"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(status, 1)
        self.assertEqual((track["bpm"], track["bpm_source"]), (128, "youtube"))
        self.assertEqual(source_count, 0)

    def test_audit_interrupt_returns_shell_interrupt_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.execute(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, bpm, "
                " bpm_source, bpm_needs_review, bpm_verified, key) "
                "VALUES (10, 1, 1, 'Legacy', '5:00', 128, 'youtube', 0, 0, 'Am')"
            )
            conn.commit()
            conn.close()

            with (
                patch.object(
                    audit_bpm,
                    "parse_arguments",
                    return_value=SimpleNamespace(limit=None),
                ),
                patch.object(audit_bpm, "init_db"),
                patch.object(
                    audit_bpm,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(
                    audit_bpm,
                    "analyze_track",
                    side_effect=KeyboardInterrupt,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = audit_bpm.main()

        self.assertEqual(status, 130)

    def test_retryable_failure_keeps_release_pending_and_preserves_prior_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.executemany(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, key) "
                "VALUES (?, 1, ?, ?, '5:00', 'Am')",
                [(10, 1, "First"), (20, 2, "Second")],
            )
            conn.commit()
            conn.close()

            success_source = analyze_bpm.AudioSource(
                platform="youtube",
                title="Artist - First",
                url="https://youtube.example/first",
            )
            retryable_source = analyze_bpm.AudioSource(
                note="all providers unavailable",
                analysis_version=None,
                retryable=True,
            )
            with (
                patch.object(
                    analyze_bpm,
                    "parse_arguments",
                    return_value=SimpleNamespace(all=False, limit=None, pace=0),
                ),
                patch.object(analyze_bpm, "init_db"),
                patch.object(
                    analyze_bpm,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(analyze_bpm, "mark_workflow_step") as mark_step,
                patch.object(
                    analyze_bpm,
                    "analyze_track",
                    side_effect=[
                        (analyze_bpm.AudioAnalysis(bpm=128), success_source),
                        (analyze_bpm.AudioAnalysis(), retryable_source),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = analyze_bpm.main()

            conn = connect(path)
            tracks = {
                row["id"]: row["bpm"]
                for row in conn.execute("SELECT id, bpm FROM tracks ORDER BY id")
            }
            conn.close()

        self.assertEqual(status, 1)
        self.assertEqual(tracks, {10: 128, 20: None})
        mark_step.assert_not_called()

    def test_clean_empty_search_is_attempted_and_can_complete_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.execute(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, key) "
                "VALUES (10, 1, 1, 'Track', '5:00', 'Am')"
            )
            conn.commit()
            conn.close()

            clean_miss = analyze_bpm.AudioSource(
                note="no matching result",
                analysis_version=None,
            )
            with (
                patch.object(
                    analyze_bpm,
                    "parse_arguments",
                    return_value=SimpleNamespace(all=False, limit=None, pace=0),
                ),
                patch.object(analyze_bpm, "init_db"),
                patch.object(
                    analyze_bpm,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(analyze_bpm, "mark_workflow_step") as mark_step,
                patch.object(
                    analyze_bpm,
                    "analyze_track",
                    return_value=(analyze_bpm.AudioAnalysis(), clean_miss),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = analyze_bpm.main()

        self.assertEqual(status, 0)
        mark_step.assert_called_once_with(ANY, 1, "analyze")

    def test_limited_clean_miss_batches_advance_to_later_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO releases VALUES (1, 'Artist', 'Release', 'CAT001')"
            )
            conn.executemany(
                "INSERT INTO tracks "
                "(id, release_id, sort_order, title, duration_display, key) "
                "VALUES (?, 1, ?, ?, '5:00', 'Am')",
                [(10, 1, "First"), (20, 2, "Second")],
            )
            conn.commit()
            conn.close()

            analyzed_titles = []

            def clean_miss(_artist, title, *_args, **_kwargs):
                analyzed_titles.append(title)
                return (
                    analyze_bpm.AudioAnalysis(),
                    analyze_bpm.AudioSource(
                        note="no matching result",
                        analysis_version=None,
                    ),
                )

            with (
                patch.object(
                    analyze_bpm,
                    "parse_arguments",
                    return_value=SimpleNamespace(all=False, limit=1, pace=0),
                ),
                patch.object(analyze_bpm, "init_db"),
                patch.object(
                    analyze_bpm,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(analyze_bpm, "analyze_track", side_effect=clean_miss),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                first_status = analyze_bpm.main()
                second_status = analyze_bpm.main()

            conn = connect(path)
            track_attempts = conn.execute(
                "SELECT COUNT(*) FROM track_workflow_steps WHERE step = 'analyze'"
            ).fetchone()[0]
            release_attempts = conn.execute(
                "SELECT COUNT(*) FROM workflow_steps WHERE step = 'analyze'"
            ).fetchone()[0]
            conn.close()

        self.assertEqual((first_status, second_status), (0, 0))
        self.assertEqual(analyzed_titles, ["First", "Second"])
        self.assertEqual(track_attempts, 2)
        self.assertEqual(release_attempts, 1)


if __name__ == "__main__":
    unittest.main()
