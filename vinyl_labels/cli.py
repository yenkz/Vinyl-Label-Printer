"""One predictable command-line front door for the project scripts."""

import argparse
import subprocess
import sys

from .paths import PROJECT_ROOT

COMMANDS = {
    "fetch": "vinyl_labels.commands.fetch_discogs",
    "beatport": "vinyl_labels.commands.enrich_beatport",
    "bandcamp": "vinyl_labels.commands.enrich_bandcamp",
    "spotify": "vinyl_labels.commands.enrich_spotify",
    "analyze": "vinyl_labels.commands.analyze_bpm",
    "audit": "vinyl_labels.commands.audit_bpm",
    "edit": "vinyl_labels.commands.edit_bpm",
    "export": "vinyl_labels.commands.bpm_manual",
    "import": "vinyl_labels.commands.bpm_manual",
    "render": "vinyl_labels.commands.render_labels",
    "print": "vinyl_labels.commands.print_labels",
    "download": "vinyl_labels.commands.download_music",
    "slskd-status": "vinyl_labels.commands.slskd_monitor",
}


def run(command, arguments=()):
    """Run one existing command with this interpreter and return its status."""
    module = COMMANDS[command]
    extra = list(arguments)
    if command in {"export", "import"}:
        extra.insert(0, command)
    return subprocess.run(
        [sys.executable, "-m", module, *extra],
        cwd=PROJECT_ROOT,
        check=False,
    ).returncode


def run_workflow(arguments):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels workflow",
        description="Run every automatic label stage sequentially.",
    )
    parser.add_argument("--all", action="store_true", help="revisit the full collection")
    parser.add_argument("--limit", type=int, help="maximum tracks for Beatport/audio stages")
    parser.add_argument("--pace", type=float, help="seconds between audio-analysis tracks")
    parser.add_argument("--skip-spotify", action="store_true")
    parser.add_argument("--skip-analyze", action="store_true")
    args = parser.parse_args(arguments)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.pace is not None and args.pace < 0:
        parser.error("--pace must be zero or more seconds")

    common = ["--all"] if args.all else []
    stages = [("fetch", common), ("beatport", ([str(args.limit)] if args.limit else []) + common)]
    stages.append(("bandcamp", common))
    if not args.skip_spotify:
        stages.append(("spotify", common))
    if not args.skip_analyze:
        analyze_args = ([str(args.limit)] if args.limit else []) + common
        if args.pace is not None:
            analyze_args += ["--pace", str(args.pace)]
        stages.append(("analyze", analyze_args))
    stages.append(("render", common))

    for command, stage_args in stages:
        print(f"\n==> {command}", flush=True)
        status = run(command, stage_args)
        if status:
            print(f"Workflow stopped: {command} exited with status {status}.", file=sys.stderr)
            return status
    return 0


def run_checks():
    """Run the same lint and test gates used by Make and CI."""
    commands = (
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "unittest", "discover", "-v"],
    )
    for command in commands:
        status = subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode
        if status:
            return status
    return 0


def main(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels",
        description="Manage a Discogs-backed vinyl label workflow.",
    )
    choices = sorted([*COMMANDS, "workflow", "check", "backup"])
    parser.add_argument("command", choices=choices)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)

    if args.command == "workflow":
        return run_workflow(args.arguments)
    if args.command == "check":
        return run_checks()
    if args.command == "backup":
        if args.arguments:
            parser.error("backup does not accept additional arguments")
        from .db import backup_database

        print(backup_database())
        return 0
    return run(args.command, args.arguments)
