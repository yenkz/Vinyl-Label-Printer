import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from vinyl_labels import db
from vinyl_labels.commands import analyze_bpm


class ChromaKeyTests(unittest.TestCase):
    def test_major_profile_rotation(self):
        key, score, margin = analyze_bpm.estimate_key_from_chroma(
            np.roll(analyze_bpm.KRUMHANSL_MAJOR, 9)
        )
        self.assertEqual(key, "A")
        self.assertAlmostEqual(score, 1.0)
        self.assertGreater(margin, 0)

    def test_minor_profile_rotation(self):
        key, score, margin = analyze_bpm.estimate_key_from_chroma(
            np.roll(analyze_bpm.KRUMHANSL_MINOR, 1)
        )
        self.assertEqual(key, "C#m")
        self.assertAlmostEqual(score, 1.0)
        self.assertGreater(margin, 0)

    def test_exact_detector_agreement_is_confirmed(self):
        with (
            patch.object(analyze_bpm, "measure_key_essentia", return_value=("Am", 0.8)),
            patch.object(analyze_bpm, "measure_key_librosa", return_value=("Am", 0.7)),
        ):
            key, alternative, doubtful, strength, estimates = analyze_bpm.measure_key("x")
        self.assertEqual((key, alternative, doubtful, strength), ("Am", None, False, 0.8))
        self.assertEqual([estimate.source for estimate in estimates], ["essentia", "librosa"])

    def test_detector_disagreement_keeps_both_candidates(self):
        with (
            patch.object(analyze_bpm, "measure_key_essentia", return_value=("Am", 0.8)),
            patch.object(analyze_bpm, "measure_key_librosa", return_value=("C", 0.7)),
        ):
            key, alternative, doubtful, _strength, _estimates = analyze_bpm.measure_key("x")
        self.assertEqual((key, alternative, doubtful), ("Am", "C", True))

    def test_librosa_is_used_only_when_essentia_is_unavailable(self):
        with (
            patch.object(analyze_bpm, "measure_key_essentia", return_value=(None, None)),
            patch.object(analyze_bpm, "measure_key_librosa", return_value=("C", 0.7)),
        ):
            result = analyze_bpm.measure_audio("x", 180, need_bpm=False, need_key=True)
        self.assertEqual((result.key, result.key_source), ("C", "librosa"))

    def test_essentia_is_selected_ahead_of_librosa(self):
        with (
            patch.object(analyze_bpm, "measure_key_essentia", return_value=("Am", 0.8)),
            patch.object(analyze_bpm, "measure_key_librosa", return_value=("C", 0.7)),
        ):
            result = analyze_bpm.measure_audio("x", 180, need_bpm=False, need_key=True)
        self.assertEqual((result.key, result.key_source), ("Am", "essentia"))

    def test_existing_bpm_skips_bpm_detectors(self):
        with (
            patch.object(analyze_bpm, "measure_bpm") as bpm,
            patch.object(
                analyze_bpm,
                "measure_key",
                return_value=("Am", None, False, 0.8, []),
            ) as key,
        ):
            result = analyze_bpm.measure_audio("x", 180, need_bpm=False, need_key=True)
        bpm.assert_not_called()
        key.assert_called_once_with("x")
        self.assertEqual(result.key, "Am")


class AnalyzeArgumentsTests(unittest.TestCase):
    def test_default_pace_preserves_existing_delay(self):
        args = analyze_bpm.parse_arguments([])
        self.assertEqual(args.pace, 3.0)
        self.assertIsNone(args.limit)

    def test_batch_limit_and_pace_can_be_combined(self):
        args = analyze_bpm.parse_arguments(["20", "--pace", "8.5", "--all"])
        self.assertEqual(args.limit, 20)
        self.assertEqual(args.pace, 8.5)
        self.assertTrue(args.all)

    def test_zero_pace_is_allowed(self):
        self.assertEqual(analyze_bpm.parse_arguments(["--pace", "0"]).pace, 0.0)

    def test_negative_pace_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                analyze_bpm.parse_arguments(["--pace", "-1"])


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_keys_are_migrated_to_key_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE releases (release_id INTEGER PRIMARY KEY);
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER NOT NULL,
                    bpm REAL,
                    bpm_source TEXT,
                    key TEXT,
                    key_source TEXT
                );
                INSERT INTO releases (release_id) VALUES (1), (2);
                INSERT INTO tracks (id, release_id, key, key_source)
                VALUES (10, 1, 'Am', 'beatport');
                INSERT INTO tracks (id, release_id, key, key_source)
                VALUES (20, 2, NULL, NULL);
                """
            )
            conn.commit()
            conn.close()

            with patch.object(db, "DB_PATH", path):
                db.init_db()
                migrated = db.get_connection()
                row = migrated.execute(
                    "SELECT key, source FROM key_sources WHERE track_id = 10"
                ).fetchone()
                track = migrated.execute(
                    "SELECT key_verified, key_needs_review FROM tracks WHERE id = 10"
                ).fetchone()
                analyze_steps = {
                    row["release_id"]
                    for row in migrated.execute(
                        "SELECT release_id FROM workflow_steps WHERE step = 'analyze'"
                    )
                }
                migrated.close()

            self.assertEqual((row["key"], row["source"]), ("Am", "beatport"))
            self.assertEqual((track["key_verified"], track["key_needs_review"]), (1, 0))
            self.assertIn(1, analyze_steps)
            self.assertNotIn(2, analyze_steps)


if __name__ == "__main__":
    unittest.main()
