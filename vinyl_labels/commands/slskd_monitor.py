"""
slskd_monitor.py — is slskd actually logged in to Soulseek?

Why this exists
---------------
Docker's own health check for slskd only proves the web UI answers — it stays
green even while slskd is stuck in a login loop, disconnected from Soulseek and
downloading nothing (connect, wait 5s, time out, reconnect, repeat, with the
backoff growing to over a minute between tries). The one signal that actually
matters is slskd's report of whether it is LOGGED IN to the Soulseek server,
which this script reads straight from slskd's API.

Two ways to use it
------------------
    python -m vinyl_labels slskd-status            # one-shot
                                       #   (exit code 0 = logged in, 1 = not)
    python -m vinyl_labels slskd-status --watch    # keep watching
                                       #   macOS notification when it drops off
                                       #   Soulseek, and restart the container to
                                       #   reset the backoff if it stays stuck

Watch-mode options:
    --interval N       seconds between checks (default 30)
    --stuck-after N    seconds off Soulseek before it counts as "stuck" and we
                       notify / restart (default 180)
    --no-restart       only watch and notify; never restart the container
    --container NAME   Docker container to restart (default "slskd")

A restart is the proven fix for the transient case: it throws away slskd's
grown-huge reconnect backoff and forces an immediate clean login. slskd persists
its queue across a restart, so nothing you've queued is lost.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime

import slskd_api

from vinyl_labels import config

# Don't restart more often than this, so a genuinely-down Soulseek server (where
# restarting won't help) can't turn into a container-restart loop.
RESTART_COOLDOWN_S = 300


def client():
    """A slskd API client, built from the same settings download_music.py uses."""
    return slskd_api.SlskdClient(
        host=config.SLSKD_HOST,
        api_key=config.SLSKD_API_KEY,
        url_base=config.SLSKD_URL_BASE,
    )


def status(c):
    """Reads slskd's view of its Soulseek connection. Returns a dict:
        reachable    — could we talk to slskd at all?
        logged_in    — is slskd logged in to the Soulseek server?
        reconnecting — is slskd sitting in its reconnect backoff?
        state        — slskd's own state string, e.g. 'Connected, LoggedIn'
    """
    try:
        app = c.application.state()
    except Exception as e:
        return {"reachable": False, "logged_in": False, "reconnecting": False,
                "state": str(e).splitlines()[0] if str(e) else "unreachable"}
    server = app.get("server") or {}
    state = server.get("state") or ""
    logged_in = bool(server.get("isLoggedIn")) or "LoggedIn" in state
    return {
        "reachable": True,
        "logged_in": logged_in,
        "reconnecting": bool(app.get("pendingReconnect")),
        "state": state or "unknown",
    }


def describe(s):
    """One human-readable line for a status dict."""
    if not s["reachable"]:
        return f"can't reach slskd ({s['state']}) — is the container up and Docker running?"
    if s["logged_in"]:
        return "logged in to Soulseek"
    if s["reconnecting"]:
        return f"dropped off Soulseek — retrying (reconnect backoff; state: {s['state']})"
    return f"NOT logged in to Soulseek (state: {s['state']})"


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def notify(title, message):
    """Best-effort macOS notification; a no-op on other systems or if it fails."""
    if sys.platform != "darwin":
        return
    osascript = shutil.which("osascript")
    if not osascript:
        return
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run([osascript, "-e", script], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def find_docker():
    """Docker isn't always on PATH from a plain shell; fall back to its usual
    spots on a Mac (Intel /usr/local, Apple Silicon /opt/homebrew)."""
    for candidate in ("docker", "/usr/local/bin/docker", "/opt/homebrew/bin/docker"):
        found = shutil.which(candidate)  # accepts a bare name (searches PATH) or an absolute path
        if found:
            return found
    return None


def restart_container(name):
    """Restarts the slskd Docker container; True if it went through. Only touches
    a container that actually exists, so a binary/non-Docker setup is left alone."""
    docker = find_docker()
    if not docker:
        log("wanted to restart, but Docker isn't on PATH — skipping")
        return False
    exists = subprocess.run([docker, "inspect", name], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exists.returncode != 0:
        log(f"wanted to restart, but no Docker container named '{name}' — skipping")
        return False
    result = subprocess.run([docker, "restart", name], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    if result.returncode == 0:
        return True
    log(f"restart failed: {result.stderr.decode(errors='replace').strip()}")
    return False


def watch(c, interval, stuck_after, do_restart, container):
    log(f"watching slskd every {interval}s "
        f"(flag as stuck after {fmt_duration(stuck_after)}"
        f"{'; will restart ' + repr(container) if do_restart else '; restart disabled'}). "
        "Ctrl+C to stop.")
    last_desc = None
    unhealthy_since = None
    notified = False
    last_restart = 0.0
    while True:
        s = status(c)
        desc = describe(s)
        now = time.time()
        if desc != last_desc:
            log(desc)
            last_desc = desc
        if s["logged_in"]:
            if unhealthy_since is not None:
                back = fmt_duration(now - unhealthy_since)
                log(f"recovered after {back}")
                notify("slskd back online", f"Logged in to Soulseek again after {back}.")
            unhealthy_since, notified = None, False
        else:
            if unhealthy_since is None:
                unhealthy_since = now
            down_for = now - unhealthy_since
            if down_for >= stuck_after:
                if not notified:
                    notify("slskd lost Soulseek", f"{desc}\nOff Soulseek for {fmt_duration(down_for)}.")
                    notified = True
                if do_restart and (now - last_restart) >= RESTART_COOLDOWN_S:
                    if restart_container(container):
                        log(f"restarted '{container}' to reset the reconnect backoff")
                        notify("slskd restarted", "Was stuck off Soulseek; restarted the container.")
                        last_restart = now
                        unhealthy_since, notified = now, False  # give it fresh time to come up
        time.sleep(interval)


def main(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels slskd-status",
        description="Check whether slskd is logged in to Soulseek.",
    )
    parser.add_argument("--watch", action="store_true", help="keep watching instead of a one-shot check")
    parser.add_argument("--interval", type=int, default=30, help="seconds between checks (watch mode)")
    parser.add_argument("--stuck-after", type=int, default=180,
                        help="seconds off Soulseek before notifying/restarting (watch mode)")
    parser.add_argument("--no-restart", action="store_true", help="watch and notify only; never restart")
    parser.add_argument("--container", default="slskd", help="Docker container to restart")
    args = parser.parse_args(arguments)

    if not config.SLSKD_API_KEY:
        print("SLSKD_API_KEY is empty. Put slskd's API key in your .env file.")
        return 1

    c = client()

    if not args.watch:
        s = status(c)
        print(describe(s))
        return 0 if s["logged_in"] else 1

    try:
        watch(c, args.interval, args.stuck_after, not args.no_restart, args.container)
    except KeyboardInterrupt:
        print("\nStopped watching.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
