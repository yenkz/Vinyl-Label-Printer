"""
edit_bpm.py — STEP 6: BPM and tonality editor and validator

Launches a local page (only visible on your computer) with your entire
collection, to load or correct BPMs and keys by hand without exporting and
importing CSVs: find the record, click on the BPM (or key), type the value
and done — it's saved directly to the database.

This is also where you validate the values that still need review. Each track
shows all BPM sources (Beatport, YouTube measurement, or historical sources)
side by side. Discogs + Beatport matches are already confirmed automatically;
for the remaining values, use the ✓ button or click a source pill.

The key is shown in Camelot notation ("8A"), but you can write it any way:
"8A", "Am", "f# minor" — it's saved normalized. Automatic Essentia/librosa
key candidates appear below the field; detector disagreements are highlighted
and can be resolved by choosing a candidate or confirming the current one.

How to run it:
    python -m vinyl_labels edit

It opens automatically in the browser (http://localhost:8765). To close it,
go back to the terminal and press Ctrl+C.

BPMs and keys you enter here become source 'manual' and always win: neither
fetch_discogs nor automatic searchers overwrite them.
"""

import argparse
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vinyl_labels import config
from vinyl_labels.common import normalize_key, to_camelot
from vinyl_labels.db import get_connection, init_db, record_bpm_source, record_key_source
from vinyl_labels.paths import TEMPLATES_DIR, project_path

COVERS_DIR = project_path(config.COVERS_DIR)

PORT = 8765

TEMPLATE_PATH = TEMPLATES_DIR / "editor.html"
PAGE = TEMPLATE_PATH.read_text(encoding="utf-8")


def read_data():
    conn = get_connection()
    cursor = conn.cursor()

    # Every source that reported a BPM, per track (rows with bpm NULL are
    # "consulted and didn't have it": those are not shown).
    cursor.execute("SELECT track_id, source, bpm, detail FROM bpm_sources WHERE bpm IS NOT NULL")
    sources_by_track = {}
    for f in cursor.fetchall():
        sources_by_track.setdefault(f["track_id"], []).append(
            {"source": f["source"], "bpm": f["bpm"], "detail": f["detail"]}
        )

    cursor.execute(
        "SELECT track_id, source, key, strength, detail"
        " FROM key_sources WHERE key IS NOT NULL"
    )
    key_sources_by_track = {}
    for f in cursor.fetchall():
        key_sources_by_track.setdefault(f["track_id"], []).append(
            {
                "source": f["source"],
                "key": f["key"],
                "camelot": to_camelot(f["key"]),
                "strength": f["strength"],
                "detail": f["detail"],
            }
        )

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = []
    for release in cursor.fetchall():
        cursor.execute(
            "SELECT id, position, title, artist, duration_display, bpm, bpm_source,"
            "       bpm_alt, bpm_needs_review, bpm_verified, key, key_source,"
            "       key_alt, key_needs_review, key_verified, key_strength"
            " FROM tracks WHERE release_id = ? ORDER BY sort_order, id",
            (release["release_id"],),
        )
        tracks = [
            {
                "id": t["id"],
                "position": t["position"],
                "title": t["title"],
                "artist": t["artist"],
                "duration": t["duration_display"],
                "bpm": t["bpm"],
                "source": t["bpm_source"],
                "sources": sources_by_track.get(t["id"], []),
                "alt": t["bpm_alt"],
                "review": t["bpm_needs_review"] or 0,
                "verified": t["bpm_verified"] or 0,
                "key": t["key"],
                "camelot": to_camelot(t["key"]),
                "key_source": t["key_source"],
                "key_sources": key_sources_by_track.get(t["id"], []),
                "key_alt": t["key_alt"],
                "key_alt_camelot": to_camelot(t["key_alt"]),
                "key_review": t["key_needs_review"] or 0,
                "key_verified": t["key_verified"] or 0,
                "key_strength": t["key_strength"],
            }
            for t in cursor.fetchall()
        ]
        if tracks:
            # The cover is downloaded by enrich_spotify.py; we only announce
            # it if the file actually exists, to avoid showing broken images.
            has_cover = bool(release["cover_path"]) and (
                project_path(release["cover_path"])
            ).exists()
            releases.append(
                {
                    "id": release["release_id"],
                    "artist": release["artist"],
                    "title": release["title"],
                    "year": release["year"],
                    "cover": has_cover,
                    "tracks": tracks,
                }
            )
    conn.close()
    return {"releases": releases}


def save_bpm(track_id, bpm):
    # A BPM you typed yourself counts as validated (typing it IS the manual
    # validation); if you clear it, it obviously stops being validated.
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE tracks SET bpm = ?, bpm_source = ?,"
        " bpm_alt = NULL, bpm_needs_review = 0, bpm_verified = ? WHERE id = ?",
        (bpm, "manual" if bpm is not None else None, int(bpm is not None), track_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return False
    if bpm is not None:
        record_bpm_source(conn, track_id, "manual", bpm)
    else:
        conn.execute(
            "DELETE FROM bpm_sources WHERE track_id = ? AND source = 'manual'",
            (track_id,),
        )
    conn.commit()
    conn.close()
    return True


def use_source(track_id, source):
    """The user chose a source's value (click on the pill): that BPM becomes
    the track's and is validated — choosing it by hand IS the confirmation.
    Returns the BPM, or None if the source didn't have one."""
    conn = get_connection()
    row = conn.execute(
        "SELECT bpm FROM bpm_sources WHERE track_id = ? AND source = ? AND bpm IS NOT NULL",
        (track_id, source),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    cursor = conn.execute(
        "UPDATE tracks SET bpm = ?, bpm_source = ?, bpm_alt = NULL,"
        " bpm_needs_review = 0, bpm_verified = 1 WHERE id = ?",
        (row["bpm"], source, track_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return None
    conn.commit()
    conn.close()
    return row["bpm"]


def save_key(track_id, key):
    """A key you typed yourself overrides whatever was there; if you clear it,
    it's left empty (and the next enrich_beatport.py can fill it in)."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE tracks SET key = ?, key_source = ?, key_alt = NULL,"
        " key_needs_review = 0, key_verified = ?, key_strength = NULL WHERE id = ?",
        (key, "manual" if key is not None else None, int(key is not None), track_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return False
    if key is not None:
        record_key_source(conn, track_id, "manual", key)
    else:
        conn.execute(
            "DELETE FROM key_sources WHERE track_id = ? AND source = 'manual'",
            (track_id,),
        )
    conn.commit()
    conn.close()
    return True


def use_key_source(track_id, source):
    """Chooses one detected key and marks the user's choice as verified."""
    conn = get_connection()
    row = conn.execute(
        "SELECT key, strength FROM key_sources"
        " WHERE track_id = ? AND source = ? AND key IS NOT NULL",
        (track_id, source),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    cursor = conn.execute(
        "UPDATE tracks SET key = ?, key_source = ?, key_alt = NULL,"
        " key_needs_review = 0, key_verified = 1, key_strength = ? WHERE id = ?",
        (row["key"], source, row["strength"], track_id),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return None
    conn.commit()
    conn.close()
    return row["key"]


def confirm_key(track_id):
    """Confirms the currently selected automatic key."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE tracks SET key_alt = NULL, key_needs_review = 0,"
        " key_verified = 1 WHERE id = ? AND key IS NOT NULL",
        (track_id,),
    )
    confirmed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return confirmed


def confirm_bpm(track_id):
    """The user reviewed the track and the saved BPM is fine: it becomes
    validated (the source stays as it was)."""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE tracks SET bpm_alt = NULL, bpm_needs_review = 0,"
        " bpm_verified = 1 WHERE id = ? AND bpm IS NOT NULL",
        (track_id,),
    )
    confirmed = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return confirmed


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # no noise in the terminal

    def respond(self, body, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if self.path == "/":
            self.respond(PAGE, "text/html")
        elif self.path == "/api/data":
            self.respond(json.dumps(read_data()))
        elif re.fullmatch(r"/covers/\d+\.jpg", self.path):
            # Serves the covers downloaded by enrich_spotify.py. The pattern
            # only accepts "/covers/<number>.jpg", so there's no risk of
            # someone requesting other files on the machine.
            file = COVERS_DIR / Path(self.path).name
            if file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(file.read_bytes())
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in (
            "/api/bpm",
            "/api/confirm",
            "/api/key",
            "/api/source",
            "/api/key-source",
            "/api/key-confirm",
        ):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length))
            track_id = int(request["id"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            self.send_error(400)
            return

        if self.path == "/api/source":
            # Adopt a source's BPM (and validate it, because you chose it
            # looking at all the options).
            source = str(request.get("source") or "")
            bpm = use_source(track_id, source)
            if bpm is None:
                self.send_error(400)  # that source has no BPM for this track
                return
            self.respond(json.dumps({"ok": True, "bpm": bpm}))
            return

        if self.path == "/api/key-source":
            source = str(request.get("source") or "")
            key = use_key_source(track_id, source)
            if key is None:
                self.send_error(400)
                return
            self.respond(
                json.dumps({"ok": True, "key": key, "camelot": to_camelot(key)})
            )
            return

        if self.path == "/api/key-confirm":
            if not confirm_key(track_id):
                self.send_error(404)
                return
            self.respond(json.dumps({"ok": True}))
            return

        if self.path == "/api/key":
            # Accepts "8A", "Am", "f# minor"...; empty = clear it.
            text = str(request.get("key") or "").strip()
            key = normalize_key(text) if text else None
            if text and key is None:
                self.send_error(400)  # the key wasn't understood
                return
            if not save_key(track_id, key):
                self.send_error(404)
                return
            self.respond(json.dumps({"ok": True, "key": key, "camelot": to_camelot(key)}))
            return

        if self.path == "/api/confirm":
            if not confirm_bpm(track_id):
                self.send_error(404)
                return
        else:
            try:
                bpm = request.get("bpm")
                if bpm is not None:
                    bpm = float(bpm)
                    if not 30 <= bpm <= 300:
                        raise ValueError(bpm)
            except (TypeError, ValueError):
                self.send_error(400)
                return
            if not save_bpm(track_id, bpm):
                self.send_error(404)
                return
        self.respond(json.dumps({"ok": True}))


def main(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels edit",
        description="Open the local BPM and key editor.",
    )
    parser.parse_args(arguments)
    init_db()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(
            f"Port {PORT} is already in use: the editor is probably already\n"
            f"open in another terminal (or was left running from before).\n"
            f"Go to http://localhost:{PORT} — and if you just updated the\n"
            f"project, close that one with Ctrl+C and run this again."
        )
        return 1
    url = f"http://localhost:{PORT}"
    print(f"BPM & key editor open at {url}")
    print("(if it didn't open on its own, go to that address in your browser)")
    print("To stop: Ctrl+C\n")
    threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone. All changes were already saved.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
