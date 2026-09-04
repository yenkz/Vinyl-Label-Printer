import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from vinyl_labels.commands import enrich_bandcamp, enrich_beatport, enrich_spotify


class Response:
    def __init__(self, payload=None, status=200, text=""):
        self.payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class SearchOutcomeTests(unittest.TestCase):
    def test_beatport_empty_catalog_is_a_miss_but_http_failure_is_retryable(self):
        with (
            patch.object(enrich_beatport, "current_token", return_value="token"),
            patch.object(
                enrich_beatport.requests,
                "get",
                return_value=Response({"results": []}),
            ),
        ):
            self.assertIsNone(
                enrich_beatport.search_beatport("Artist", "Track", 300)
            )

        with (
            patch.object(enrich_beatport, "current_token", return_value="token"),
            patch.object(
                enrich_beatport.requests,
                "get",
                return_value=Response(status=503),
            ),
            self.assertRaises(enrich_beatport.BeatportError),
        ):
            enrich_beatport.search_beatport("Artist", "Track", 300)

    def test_bandcamp_empty_catalog_is_a_miss_but_transport_failure_is_retryable(self):
        with patch.object(
            enrich_bandcamp.requests,
            "post",
            return_value=Response({"auto": {"results": []}}),
        ):
            self.assertIsNone(
                enrich_bandcamp.search_album_bandcamp("Artist", "Release")
            )

        with (
            patch.object(
                enrich_bandcamp.requests,
                "post",
                side_effect=requests.ConnectionError("offline"),
            ),
            self.assertRaises(enrich_bandcamp.BandcampError),
        ):
            enrich_bandcamp.search_album_bandcamp("Artist", "Release")

    def test_spotify_empty_catalog_is_a_miss_but_http_failure_is_retryable(self):
        with patch.object(
            enrich_spotify.requests,
            "get",
            return_value=Response({"albums": {"items": []}}),
        ):
            self.assertIsNone(
                enrich_spotify.search_album_spotify({}, "Artist", "Release")
            )

        with (
            patch.object(
                enrich_spotify.requests,
                "get",
                return_value=Response(status=429),
            ),
            self.assertRaises(enrich_spotify.SpotifyError),
        ):
            enrich_spotify.search_album_spotify({}, "Artist", "Release")

    def test_spotify_track_search_uses_market_and_returns_exact_match(self):
        candidate = {
            "id": "spotify-track",
            "name": "The Track",
            "artists": [{"name": "The Artist"}],
            "album": {"name": "Another Release"},
            "duration_ms": 245_400,
            "external_ids": {"isrc": "GB-AAA-26-00001"},
        }
        with (
            patch.object(enrich_spotify.config, "SPOTIFY_MARKET", "ES"),
            patch.object(
                enrich_spotify.requests,
                "get",
                return_value=Response({"tracks": {"items": [candidate]}}),
            ) as request,
        ):
            result = enrich_spotify.search_track_spotify(
                {}, "The Artist", "The Track", "Original EP"
            )

        self.assertEqual(result["duration_seconds"], 245)
        self.assertEqual(result["isrc"], "GB-AAA-26-00001")
        self.assertEqual(request.call_args.kwargs["params"]["market"], "ES")
        self.assertEqual(request.call_args.kwargs["params"]["type"], "track")
        self.assertEqual(
            request.call_args.kwargs["params"]["q"],
            "track:The Track artist:The Artist",
        )

    def test_spotify_track_search_does_not_confuse_remixes(self):
        candidate = {
            "id": "wrong-version",
            "name": "The Track (Radio Edit)",
            "artists": [{"name": "The Artist"}],
            "album": {"name": "The Track"},
            "duration_ms": 180_000,
            "external_ids": {},
        }
        with patch.object(
            enrich_spotify.requests,
            "get",
            return_value=Response({"tracks": {"items": [candidate]}}),
        ):
            result = enrich_spotify.search_track_spotify(
                {}, "The Artist", "The Track", "Original EP"
            )
        self.assertIsNone(result)

    def test_spotify_track_selection_rejects_ambiguous_versions(self):
        candidates = [
            {
                "id": "short",
                "title": "The Track",
                "artists": ["The Artist"],
                "album": "Compilation One",
                "duration_seconds": 180,
                "isrc": None,
            },
            {
                "id": "long",
                "title": "The Track",
                "artists": ["The Artist"],
                "album": "Compilation Two",
                "duration_seconds": 360,
                "isrc": None,
            },
        ]
        self.assertIsNone(
            enrich_spotify.choose_spotify_track(
                candidates, "The Artist", "The Track", "Original EP"
            )
        )

    def test_spotify_track_selection_prefers_the_matching_release(self):
        candidates = [
            {
                "id": "compilation",
                "title": "The Track",
                "artists": ["The Artist"],
                "album": "Compilation",
                "duration_seconds": 180,
                "isrc": None,
            },
            {
                "id": "release",
                "title": "The Track",
                "artists": ["The Artist"],
                "album": "Original EP",
                "duration_seconds": 360,
                "isrc": None,
            },
        ]
        result = enrich_spotify.choose_spotify_track(
            candidates, "The Artist", "The Track", "Original EP"
        )
        self.assertEqual(result["id"], "release")

    def test_spotify_track_selection_accepts_an_exact_isrc_despite_title_formatting(self):
        candidate = {
            "id": "isrc-match",
            "title": "The Track - Remastered",
            "artists": ["Different Display Name"],
            "album": "Different Release",
            "duration_seconds": 245,
            "isrc": "GB-AAA-26-00001",
        }
        result = enrich_spotify.choose_spotify_track(
            [candidate],
            "The Artist",
            "The Track",
            "Original EP",
            "GBAAA2600001",
        )
        self.assertEqual(result["id"], "isrc-match")


def create_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE releases (
            release_id INTEGER PRIMARY KEY,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            cover_path TEXT,
            catno TEXT
        );
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            release_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            artist TEXT,
            duration_display TEXT,
            bpm REAL,
            bpm_source TEXT,
            bpm_alt REAL,
            bpm_needs_review INTEGER NOT NULL DEFAULT 0,
            bpm_verified INTEGER NOT NULL DEFAULT 0,
            key TEXT,
            key_source TEXT,
            key_alt TEXT,
            key_needs_review INTEGER NOT NULL DEFAULT 0,
            key_verified INTEGER NOT NULL DEFAULT 0,
            key_strength REAL,
            isrc TEXT
        );
        CREATE TABLE bpm_sources (
            track_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            bpm REAL,
            detail TEXT,
            UNIQUE(track_id, source)
        );
        CREATE TABLE key_sources (
            track_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            key TEXT,
            strength REAL,
            detail TEXT,
            UNIQUE(track_id, source)
        );
        CREATE TABLE workflow_steps (
            release_id INTEGER NOT NULL,
            step TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(release_id, step)
        );
        INSERT INTO releases (release_id, artist, title)
        VALUES (1, 'Artist', 'Release');
        INSERT INTO tracks (id, release_id, sort_order, title)
        VALUES (10, 1, 1, 'Track');
        """
    )
    conn.commit()
    conn.close()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class WorkflowFailureTests(unittest.TestCase):
    def run_with_database(self, module, behavior, *, credentials=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            patches = [
                patch.object(module, "init_db"),
                patch.object(module, "get_connection", side_effect=lambda: connect(path)),
                patch.object(module.time, "sleep"),
            ]
            if module is enrich_beatport:
                search_patch = (
                    patch.object(module, "search_beatport", side_effect=behavior)
                    if isinstance(behavior, Exception)
                    else patch.object(module, "search_beatport", return_value=behavior)
                )
                patches.extend(
                    [
                        patch.object(module, "current_token", return_value="token"),
                        search_patch,
                    ]
                )
            elif module is enrich_bandcamp:
                patches.append(
                    patch.object(
                        module,
                        "search_album_bandcamp",
                        **(
                            {"side_effect": behavior}
                            if isinstance(behavior, Exception)
                            else {"return_value": behavior}
                        ),
                    )
                )
            else:
                search_patch = (
                    patch.object(module, "search_album_spotify", side_effect=behavior)
                    if isinstance(behavior, Exception)
                    else patch.object(
                        module, "search_album_spotify", return_value=behavior
                    )
                )
                patches.extend(
                    [
                        patch.object(
                            module.config,
                            "SPOTIFY_CLIENT_ID",
                            "client" if credentials else "",
                        ),
                        patch.object(
                            module.config,
                            "SPOTIFY_CLIENT_SECRET",
                            "secret" if credentials else "",
                        ),
                        patch.object(module, "get_spotify_token", return_value="token"),
                        search_patch,
                        patch.object(module, "search_track_spotify", return_value=None),
                    ]
                )

            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                status = module.main([])

            conn = connect(path)
            steps = [
                row["step"]
                for row in conn.execute(
                    "SELECT step FROM workflow_steps ORDER BY step"
                )
            ]
            misses = [
                (row["source"], row["bpm"])
                for row in conn.execute(
                    "SELECT source, bpm FROM bpm_sources ORDER BY source"
                )
            ]
            conn.close()
            return status, steps, misses

    def test_beatport_transient_failure_is_not_a_permanent_miss(self):
        status, steps, misses = self.run_with_database(
            enrich_beatport,
            enrich_beatport.BeatportError("temporary outage"),
        )
        self.assertEqual(status, 1)
        self.assertEqual(steps, [])
        self.assertEqual(misses, [])

    def test_beatport_genuine_miss_is_recorded_and_completed(self):
        status, steps, misses = self.run_with_database(enrich_beatport, None)
        self.assertEqual(status, 0)
        self.assertEqual(steps, ["beatport"])
        self.assertEqual(misses, [("beatport", None)])

    def test_beatport_replaces_a_differing_verified_audio_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "UPDATE tracks SET key = 'Am', key_source = 'audio', "
                "key_verified = 1 WHERE id = 10"
            )
            conn.commit()
            conn.close()

            candidate = {
                "name": "Track",
                "mix_name": "Original Mix",
                "key": {"name": "C Minor"},
            }
            with (
                patch.object(enrich_beatport, "init_db"),
                patch.object(
                    enrich_beatport,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(enrich_beatport, "current_token", return_value="token"),
                patch.object(
                    enrich_beatport, "search_beatport", return_value=candidate
                ),
                patch.object(enrich_beatport.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = enrich_beatport.main([])

            conn = connect(path)
            track = conn.execute(
                "SELECT key, key_source, key_verified FROM tracks WHERE id = 10"
            ).fetchone()
            beatport = conn.execute(
                "SELECT key FROM key_sources "
                "WHERE track_id = 10 AND source = 'beatport'"
            ).fetchone()
            conn.close()

        self.assertEqual(status, 0)
        self.assertEqual(
            (track["key"], track["key_source"], track["key_verified"]),
            ("Cm", "beatport", 1),
        )
        self.assertEqual(beatport["key"], "Cm")

    def test_bandcamp_transient_failure_keeps_release_pending(self):
        status, steps, _misses = self.run_with_database(
            enrich_bandcamp,
            enrich_bandcamp.BandcampError("temporary outage"),
        )
        self.assertEqual(status, 1)
        self.assertEqual(steps, [])

    def test_bandcamp_genuine_miss_completes_the_attempt(self):
        status, steps, _misses = self.run_with_database(enrich_bandcamp, None)
        self.assertEqual(status, 0)
        self.assertEqual(steps, ["bandcamp"])

    def test_spotify_transient_failure_keeps_release_pending(self):
        status, steps, _misses = self.run_with_database(
            enrich_spotify,
            enrich_spotify.SpotifyError("temporary outage"),
        )
        self.assertEqual(status, 1)
        self.assertEqual(steps, [])

    def test_spotify_genuine_miss_completes_the_attempt(self):
        status, steps, _misses = self.run_with_database(enrich_spotify, None)
        self.assertEqual(status, 0)
        self.assertEqual(steps, ["spotify"])

    def test_spotify_album_miss_falls_back_only_for_missing_track_durations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO tracks"
                " (id, release_id, sort_order, title, duration_display)"
                " VALUES (20, 1, 2, 'Already Timed', '4:00')"
            )
            conn.commit()
            conn.close()

            match = {
                "id": "spotify-track",
                "title": "Track",
                "artists": ["Artist"],
                "album": "Compilation",
                "duration_seconds": 245,
                "isrc": "GB-AAA-26-00001",
            }
            with (
                patch.object(enrich_spotify, "init_db"),
                patch.object(
                    enrich_spotify,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_ID", "client"),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_SECRET", "secret"),
                patch.object(enrich_spotify, "get_spotify_token", return_value="token"),
                patch.object(enrich_spotify, "search_album_spotify", return_value=None),
                patch.object(
                    enrich_spotify, "search_track_spotify", return_value=match
                ) as track_search,
                patch.object(enrich_spotify.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = enrich_spotify.main([])

            conn = connect(path)
            tracks = {
                row["id"]: (row["duration_display"], row["isrc"])
                for row in conn.execute(
                    "SELECT id, duration_display, isrc FROM tracks ORDER BY id"
                )
            }
            steps = [row["step"] for row in conn.execute("SELECT step FROM workflow_steps")]
            conn.close()

        self.assertEqual(status, 0)
        self.assertEqual(tracks[10], ("4:05", "GB-AAA-26-00001"))
        self.assertEqual(tracks[20], ("4:00", None))
        self.assertEqual(steps, ["spotify"])
        track_search.assert_called_once_with(
            {"Authorization": "Bearer token"}, "Artist", "Track", "Release", None
        )

    def test_spotify_track_search_failure_keeps_release_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            with (
                patch.object(enrich_spotify, "init_db"),
                patch.object(
                    enrich_spotify,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_ID", "client"),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_SECRET", "secret"),
                patch.object(enrich_spotify, "get_spotify_token", return_value="token"),
                patch.object(enrich_spotify, "search_album_spotify", return_value=None),
                patch.object(
                    enrich_spotify,
                    "search_track_spotify",
                    side_effect=enrich_spotify.SpotifyError("temporary outage"),
                ),
                patch.object(enrich_spotify.time, "sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = enrich_spotify.main([])

            conn = connect(path)
            steps = list(conn.execute("SELECT * FROM workflow_steps"))
            conn.close()

        self.assertEqual(status, 1)
        self.assertEqual(steps, [])

    def test_missing_spotify_credentials_are_an_optional_clean_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            with (
                patch.object(enrich_spotify, "init_db"),
                patch.object(
                    enrich_spotify,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_ID", ""),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_SECRET", ""),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = enrich_spotify.main([])
        self.assertEqual(status, 0)

    def test_spotify_authentication_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.db"
            create_database(path)
            with (
                patch.object(enrich_spotify, "init_db"),
                patch.object(
                    enrich_spotify,
                    "get_connection",
                    side_effect=lambda: connect(path),
                ),
                patch.object(enrich_spotify.config, "SPOTIFY_CLIENT_ID", "client"),
                patch.object(
                    enrich_spotify.config, "SPOTIFY_CLIENT_SECRET", "secret"
                ),
                patch.object(
                    enrich_spotify,
                    "get_spotify_token",
                    side_effect=enrich_spotify.SpotifyError("bad credentials"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                status = enrich_spotify.main([])

            conn = connect(path)
            steps = list(conn.execute("SELECT * FROM workflow_steps"))
            conn.close()

        self.assertEqual(status, 1)
        self.assertEqual(steps, [])


if __name__ == "__main__":
    unittest.main()
