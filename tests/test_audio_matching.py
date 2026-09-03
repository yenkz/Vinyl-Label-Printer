import unittest

from vinyl_labels import audio_matching
from vinyl_labels.commands import analyze_bpm


class QueryMatchingTests(unittest.TestCase):
    def test_composite_artists_and_catalog_number_produce_independent_queries(self):
        self.assertEqual(
            audio_matching.build_queries("B.Love / Jhobei", "Snapshot", "SEMID026"),
            [
                "B.Love Snapshot",
                "Jhobei Snapshot",
                "SEMID026 Snapshot",
            ],
        )

    def test_various_artist_uses_title_and_catalog_queries(self):
        self.assertEqual(
            audio_matching.build_queries("Various", "Sampler", "CAT001"),
            ["Sampler", "CAT001 Sampler"],
        )

    def test_title_matching_retains_compact_fuzzy_behavior(self):
        self.assertTrue(
            audio_matching.title_matches("Sugar Coated", "Artist - Sugarcoated")
        )
        self.assertFalse(audio_matching.title_matches("Sugar Coated", "Other Song"))


class VideoSelectionTests(unittest.TestCase):
    def test_candidates_are_filtered_and_sorted_without_admitting_compilations(self):
        nearest = {
            "title": "Artist - Track",
            "uploader": "Label",
            "duration": 302,
        }
        approved_but_further = {
            "title": "Track (Original Mix)",
            "uploader": "Artist",
            "duration": 330,
        }
        rescue = {
            "title": "Artist - Track (Extended)",
            "uploader": "Label",
            "duration": 400,
        }
        compilation = {
            "title": "Artist - Track EP Preview",
            "uploader": "Artist",
            "duration": 420,
        }
        wrong_artist = {
            "title": "Track",
            "uploader": "Someone Else",
            "duration": 300,
        }
        wrong_title = {
            "title": "Artist - Different Song",
            "uploader": "Artist",
            "duration": 300,
        }

        approved, rescues = audio_matching.select_videos(
            [
                approved_but_further,
                wrong_artist,
                rescue,
                nearest,
                compilation,
                wrong_title,
            ],
            "Artist",
            "Track",
            300,
        )

        self.assertEqual(approved, [nearest, approved_but_further])
        self.assertEqual(rescues, [rescue])

    def test_missing_discogs_duration_accepts_only_plausible_track_lengths(self):
        plausible = {
            "title": "Artist - Track",
            "uploader": "Artist",
            "duration": 240,
        }
        too_short = {
            "title": "Artist - Track",
            "uploader": "Artist",
            "duration": 60,
        }

        approved, rescues = audio_matching.select_videos(
            [too_short, plausible], "Artist", "Track", None
        )

        self.assertEqual(approved, [plausible])
        self.assertEqual(rescues, [])


class CompatibilityExportTests(unittest.TestCase):
    def test_analyze_command_reexports_matching_api(self):
        names = (
            "artist_tokens",
            "build_queries",
            "compact",
            "partial_similarity",
            "seems_compilation",
            "select_videos",
            "split_artists",
            "title_matches",
            "words",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(analyze_bpm, name),
                    getattr(audio_matching, name),
                )

        self.assertEqual(
            analyze_bpm.TOLERANCE_SECONDS,
            audio_matching.TOLERANCE_SECONDS,
        )
        self.assertEqual(
            analyze_bpm.TOLERANCE_PERCENTAGE,
            audio_matching.TOLERANCE_PERCENTAGE,
        )


if __name__ == "__main__":
    unittest.main()
