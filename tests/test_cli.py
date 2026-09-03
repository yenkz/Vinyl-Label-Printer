import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vinyl_labels import cli


class WorkflowCliTests(unittest.TestCase):
    def test_workflow_runs_stages_in_order(self):
        calls = []

        def fake_run(command, arguments=()):
            calls.append((command, list(arguments)))
            return 0

        with patch.object(cli, "run", side_effect=fake_run):
            status = cli.run_workflow(["--limit", "4", "--pace", "0"])

        self.assertEqual(status, 0)
        self.assertEqual(
            [command for command, _arguments in calls],
            ["fetch", "beatport", "bandcamp", "spotify", "analyze", "render"],
        )
        self.assertEqual(calls[1][1], ["4"])
        self.assertEqual(calls[4][1], ["4", "--pace", "0.0"])

    def test_workflow_stops_at_first_failed_stage(self):
        with patch.object(cli, "run", side_effect=[0, 3]) as run:
            status = cli.run_workflow(["--skip-spotify", "--skip-analyze"])

        self.assertEqual(status, 3)
        self.assertEqual([call.args[0] for call in run.call_args_list], ["fetch", "beatport"])

    def test_workflow_rejects_non_positive_limit(self):
        with (
            self.assertRaises(SystemExit) as raised,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cli.run_workflow(["--limit", "0"])

        self.assertEqual(raised.exception.code, 2)

    def test_check_runs_lint_and_tests(self):
        with patch.object(
            cli.subprocess,
            "run",
            side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)],
        ) as run:
            status = cli.main(["check"])

        self.assertEqual(status, 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("ruff", run.call_args_list[0].args[0])
        self.assertIn("unittest", run.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
