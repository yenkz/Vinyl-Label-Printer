import tempfile
import unittest
from pathlib import Path

from vinyl_labels.soulseek import (
    artist_names,
    clean_query,
    format_rank,
    library_path,
    rank_candidates,
    remote_basename,
    safe_name,
    track_stem,
)


class FilenameNormalizationTests(unittest.TestCase):
    def test_remote_windows_paths_and_vinyl_positions_are_normalized(self):
        path = r"@@peer\Music\Artist\A1 - Étienne's Theme.FLAC"

        self.assertEqual(remote_basename(path), "A1 - Étienne's Theme.FLAC")
        self.assertEqual(track_stem(path), "Étienne's Theme")

    def test_queries_fold_accents_and_punctuation(self):
        self.assertEqual(clean_query("  Étienne's—Theme!  "), "Etienne sTheme")

    def test_composite_artist_credits_are_distinct_and_ignore_placeholders(self):
        self.assertEqual(
            artist_names("B.Love / Jhobei", "Various Artists / B.Love", "Unknown"),
            ["B.Love", "Jhobei"],
        )


class CandidateRankingTests(unittest.TestCase):
    def test_format_preference_and_extension_fallback(self):
        self.assertEqual(format_rank({"filename": r"x\track.aiff"}), 5)
        self.assertEqual(format_rank({"extension": ".flac"}), 4)
        self.assertEqual(format_rank({"extension": "mp3", "bitRate": 320}), 2)
        self.assertEqual(format_rank({"extension": "mp3", "bitRate": 256}), 1)
        self.assertEqual(format_rank({"extension": "ogg"}), -1)

    def test_candidates_are_filtered_ranked_and_deduplicated_by_peer(self):
        responses = [
            {
                "username": "lossless-peer",
                "hasFreeUploadSlot": False,
                "uploadSpeed": 100,
                "queueLength": 10,
                "files": [
                    {"filename": r"Music\01 - The Track.flac", "extension": "flac", "length": 301},
                    {"filename": r"Music\01 - The Track.aiff", "extension": "aiff", "length": 360},
                    {"filename": r"Music\Other Song.aiff", "extension": "aiff", "length": 300},
                ],
            },
            {
                "username": "free-peer",
                "hasFreeUploadSlot": True,
                "uploadSpeed": 1000,
                "queueLength": 0,
                "files": [
                    {"filename": r"The Track.flac", "extension": "flac", "length": 300},
                    {"filename": r"The Track.wav", "extension": "wav", "length": 300, "isLocked": True},
                    {"filename": r"The Track.ogg", "extension": "ogg", "length": 300},
                ],
            },
        ]

        ranked, unconfirmed = rank_candidates(
            responses, "The Track", target_duration=300
        )

        self.assertFalse(unconfirmed)
        self.assertEqual([username for _, username, _ in ranked], ["lossless-peer", "free-peer"])
        self.assertEqual(ranked[0][0]["extension"], "aiff")
        self.assertEqual(ranked[1][0]["extension"], "flac")

    def test_title_only_match_without_credited_artist_is_flagged_not_returned(self):
        responses = [
            {
                "username": "peer",
                "files": [
                    {"filename": r"Someone Else\The Track.flac", "extension": "flac"}
                ],
            }
        ]

        ranked, unconfirmed = rank_candidates(
            responses,
            "The Track",
            credited_artists=["Right Artist"],
            require_artist=True,
        )

        self.assertEqual(ranked, [])
        self.assertTrue(unconfirmed)


class SafeLibraryPathTests(unittest.TestCase):
    def test_safe_name_is_nonempty_and_removes_path_separators(self):
        self.assertEqual(safe_name("../A/B:*?"), "AB")
        self.assertEqual(safe_name("..."), "Unknown")

    def test_library_path_stays_below_root_and_keeps_readable_metadata(self):
        release = {
            "artist": "A/Artist",
            "title": "../Album: One",
            "catno": "CAT/01",
        }
        track = {"position": "A1", "title": "../Track?"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            destination = library_path(root, ".flac", track, release)

            self.assertTrue(destination.is_relative_to(root))
            self.assertEqual(
                destination.relative_to(root),
                Path("AArtist - ..Album One (CAT01)") / "A1 ..Track.flac",
            )
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
