"""
audit_bpm.py — Audit of BPMs measured with the old version.

Before, analyze_bpm.py measured tempo with a single detector (librosa),
which in electronic music sometimes locks onto the wrong beat:
it measures 89 where the real tempo is 134 (a 2/3 error that range
adjustment can't fix, because both numbers are valid tempos).

This script takes tracks measured by the old analysis version that you haven't
re-measured with the new version, downloads the audio again, and re-measures
them with both detectors:

  - if the new measurement matches the saved one, the re-measurement
    is noted in bpm_sources: in the editor you'll see both sources
    agree and validate it with one click (nothing validates itself:
    you always put the green checkmark);
  - if it doesn't match, saves the new value (deeprhythm, much more
    precise on electronic music) and leaves the old one as alternative,
    with the track marked as doubtful so you resolve it in the editor
    (python edit_bpm.py) — one click and done.

You can run it as many times as you like: those already re-measured with
both detectors won't be re-checked (that includes everything analyze_bpm.py
analyzes from now on).

How to run it:
    python audit_bpm.py        # audit all
    python audit_bpm.py 5      # only 5 (for testing)

You can stop with Ctrl+C: what's already reviewed is saved.
"""

import sys
import tempfile
import time

from analyze_bpm import TOLERANCE_BPM, analyze_track, parse_duration
from db import get_connection, init_db, record_bpm_source


def main():
    limit = None
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(__doc__)
            return

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    # Re-measure automatic BPMs that don't yet have a "new era" measurement:
    # bpm_sources rows with detail (the video the number came from) were written
    # by two-detector analysis; those without it come from the old version.
    cursor.execute(
        """
        SELECT tracks.id, tracks.title, tracks.duration_display, tracks.bpm,
               COALESCE(tracks.artist, releases.artist) AS artist,
               releases.catno
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm_source = 'youtube'
          AND tracks.bpm IS NOT NULL
          AND tracks.bpm_needs_review = 0
          AND tracks.bpm_verified = 0
          AND NOT EXISTS (SELECT 1 FROM bpm_sources
                          WHERE track_id = tracks.id AND source = 'youtube'
                            AND bpm IS NOT NULL AND detail IS NOT NULL)
        ORDER BY releases.artist, releases.title, tracks.id
        """
    )
    pending = cursor.fetchall()
    if limit:
        pending = pending[:limit]

    print(f"Tracks to audit: {len(pending)}")
    print("(downloads each audio again and re-measures; takes ~30s per track,")
    print(" you can stop with Ctrl+C and resume later)\n")

    confirmed = 0
    corrected = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pending, start=1):
            label = f"[{i}/{len(pending)}] {row['artist']} - {row['title']}"
            try:
                result, detail = analyze_track(
                    row["artist"], row["title"],
                    parse_duration(row["duration_display"]), tmpdir,
                    row["catno"],
                    need_bpm=True,
                    need_key=False,
                )
            except KeyboardInterrupt:
                print("\nStopped. What's been audited is saved.")
                break
            except Exception as e:
                print(f"{label}\n   -> error, continuing: {e}")
                continue

            new = result.bpm
            if new is None:
                print(f"{label}\n   -> couldn't re-measure ({detail}), keeping as is")
            elif abs(new - row["bpm"]) <= TOLERANCE_BPM:
                # Two independent measurements agree: it's noted
                # (in the editor you validate it with one click).
                record_bpm_source(conn, row["id"], "youtube", new, detail)
                conn.commit()
                confirmed += 1
                print(f"{label} -> {row['bpm']:g} BPM matches (validate in editor)")
            else:
                # The old measurement was the typical error: save the new
                # one and leave the old one one click away in the editor.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_alt = ?, bpm_needs_review = 1,"
                    " bpm_verified = 0 WHERE id = ?",
                    (new, row["bpm"], row["id"]),
                )
                record_bpm_source(conn, row["id"], "youtube", new, detail)
                conn.commit()
                corrected += 1
                print(f"{label} -> {row['bpm']:g} seems mismeasured: "
                      f"now {new:g} BPM (confirm in edit_bpm.py)")

            time.sleep(3)  # pause to not wake YouTube's anti-bot

    conn.close()
    print("\n" + "=" * 50)
    print(f"Confirmed: {confirmed} · Corrected: {corrected}")
    if corrected:
        print("Corrected ones are marked as doubtful: take a look with:")
        print("python edit_bpm.py (filter 'doubtful only')")
    if confirmed:
        print("Confirmed ones are ready to validate with one click in the editor:")
        print("python edit_bpm.py (filter 'unvalidated only')")


if __name__ == "__main__":
    main()
