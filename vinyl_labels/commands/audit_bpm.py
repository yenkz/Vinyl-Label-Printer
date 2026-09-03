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
    (python -m vinyl_labels edit) — one click and done.

You can run it as many times as you like: those already re-measured with
both detectors won't be re-checked (that includes everything analyze_bpm.py
analyzes from now on).

How to run it:
    python -m vinyl_labels audit        # audit all
    python -m vinyl_labels audit 5      # only 5 (for testing)

You can stop with Ctrl+C: what's already reviewed is saved.
"""

import argparse
import tempfile
import time

from vinyl_labels.commands.analyze_bpm import (
    LOCAL_AUDIO_SOURCES,
    TOLERANCE_BPM,
    analyze_track,
    parse_duration,
)
from vinyl_labels.db import get_connection, init_db, record_bpm_source


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels audit",
        description="Re-audit legacy automatic BPM values.",
    )
    parser.add_argument("limit", nargs="?", type=int, help="maximum tracks to audit")
    args = parser.parse_args(arguments)
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be a positive integer")
    return args


def main(arguments=None):
    limit = parse_arguments(arguments).limit

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    # Re-measure automatic BPMs that don't yet have a "new era" measurement.
    # Legacy local analysis labeled every platform as YouTube; newer analysis
    # stores Bandcamp/YouTube/SoundCloud separately. A detailed row under any
    # local platform means it has already passed through the newer analyzer.
    source_placeholders = ", ".join("?" for _ in LOCAL_AUDIO_SOURCES)
    cursor.execute(
        f"""
        SELECT tracks.id, tracks.title, tracks.duration_display, tracks.bpm,
               COALESCE(tracks.artist, releases.artist) AS artist,
               releases.catno
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm_source IN ({source_placeholders})
          AND tracks.bpm IS NOT NULL
          AND tracks.bpm_needs_review = 0
          AND tracks.bpm_verified = 0
          AND NOT EXISTS (SELECT 1 FROM bpm_sources
                          WHERE track_id = tracks.id
                            AND source IN ({source_placeholders})
                            AND bpm IS NOT NULL AND detail IS NOT NULL)
        ORDER BY releases.artist, releases.title, tracks.sort_order, tracks.id
        """,
        LOCAL_AUDIO_SOURCES + LOCAL_AUDIO_SOURCES,
    )
    pending = cursor.fetchall()
    if limit:
        pending = pending[:limit]

    print(f"Tracks to audit: {len(pending)}")
    print("(downloads each audio again and re-measures; takes ~30s per track,")
    print(" you can stop with Ctrl+C and resume later)\n")

    confirmed = 0
    corrected = 0
    errors = 0
    interrupted = False
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pending, start=1):
            label = f"[{i}/{len(pending)}] {row['artist']} - {row['title']}"
            try:
                result, source = analyze_track(
                    row["artist"], row["title"],
                    parse_duration(row["duration_display"]), tmpdir,
                    row["catno"],
                    need_bpm=True,
                    need_key=False,
                )
            except KeyboardInterrupt:
                print("\nStopped. What's been audited is saved.")
                interrupted = True
                break
            except Exception as e:
                print(f"{label}\n   -> error, continuing: {e}")
                errors += 1
                continue

            if source.retryable:
                print(f"{label}\n   -> {source.detail} (will retry), keeping as is")
                errors += 1
                continue

            new = result.bpm
            if new is None:
                print(
                    f"{label}\n   -> couldn't re-measure "
                    f"({source.detail}), keeping as is"
                )
            elif abs(new - row["bpm"]) <= TOLERANCE_BPM:
                # Two independent measurements agree: it's noted
                # (in the editor you validate it with one click).
                cursor.execute(
                    "UPDATE tracks SET bpm_source = ? WHERE id = ?",
                    (source.platform, row["id"]),
                )
                record_bpm_source(
                    conn, row["id"], source.platform, new, source.detail
                )
                conn.commit()
                confirmed += 1
                print(f"{label} -> {row['bpm']:g} BPM matches (validate in editor)")
            else:
                # The old measurement was the typical error: save the new
                # one and leave the old one one click away in the editor.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_alt = ?, bpm_needs_review = 1,"
                    " bpm_verified = 0, bpm_source = ? WHERE id = ?",
                    (new, row["bpm"], source.platform, row["id"]),
                )
                record_bpm_source(
                    conn, row["id"], source.platform, new, source.detail
                )
                conn.commit()
                corrected += 1
                print(f"{label} -> {row['bpm']:g} seems mismeasured: "
                      f"now {new:g} BPM (confirm with `python -m vinyl_labels edit`)")

            time.sleep(3)  # pause to not wake YouTube's anti-bot

    conn.close()
    print("\n" + "=" * 50)
    print(f"Confirmed: {confirmed} · Corrected: {corrected}")
    if corrected:
        print("Corrected ones are marked as doubtful: take a look with:")
        print("python -m vinyl_labels edit (filter 'doubtful only')")
    if confirmed:
        print("Confirmed ones are ready to validate with one click in the editor:")
        print("python -m vinyl_labels edit (filter 'unvalidated only')")
    if interrupted:
        return 130
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
