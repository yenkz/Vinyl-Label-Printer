"""
bpm_manual.py — STEP 7 alternative (CSV spreadsheet workflow)

Used to manually enter BPMs that automatic search didn't find. Works in two modes:

    python bpm_manual.py export
        -> Creates a bpm_pending.csv file with all tracks that still don't have
           BPM. Open it in Excel/Numbers/Google Sheets, fill the "bpm" column
           by hand (you can search the track on Shazam, Tunebat, or listen with
           a metronome), and save it as CSV again.

    python bpm_manual.py import
        -> Reads that same bpm_pending.csv file and loads the BPMs you've filled
           in back into the database.

You can alternate export/fill/import as many times as you want, as you review
records.
"""

import csv
import sys
from pathlib import Path

from db import get_connection, init_db, registrar_bpm_fuente

CSV_PATH = Path(__file__).parent / "bpm_pendientes.csv"


def export_csv():
    init_db()  # just in case you haven't run any other step yet
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT tracks.id, COALESCE(tracks.artist, releases.artist) AS artist,
               releases.title AS album,
               tracks.position, tracks.title AS track_title, tracks.bpm
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm IS NULL
        ORDER BY releases.artist, releases.title, tracks.position
        """
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No pending BPM tracks. Everything's complete!")
        return

    # utf-8-sig: the "sig" makes Excel read accents and ñ correctly.
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["track_id", "artist", "album", "position", "track_title", "bpm"])
        for row in rows:
            writer.writerow(
                [row["id"], row["artist"], row["album"], row["position"], row["track_title"], ""]
            )

    print(f"Exported: {CSV_PATH}")
    print(f"{len(rows)} pending tracks. Fill the 'bpm' column and then run:")
    print("    python bpm_manual.py import")


def import_csv():
    if not CSV_PATH.exists():
        print(f"Can't find {CSV_PATH}. Run first: python bpm_manual.py export")
        return

    init_db()  # in case database is from an older version
    conn = get_connection()
    cursor = conn.cursor()

    updated = 0
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bpm_text = (row.get("bpm") or "").strip()
            if not bpm_text:
                continue  # you haven't filled it yet, leave it for next time

            try:
                bpm = float(bpm_text)
            except ValueError:
                print(f"   Invalid BPM for track_id {row['track_id']}: '{bpm_text}' (skipping)")
                continue

            # Entering it manually IS the manual validation: it gets the checkmark,
            # and is noted as source 'manual' alongside the others.
            cursor.execute(
                "UPDATE tracks SET bpm = ?, bpm_source = 'manual', bpm_alt = NULL,"
                " bpm_needs_review = 0, bpm_verified = 1 WHERE id = ?",
                (bpm, row["track_id"]),
            )
            registrar_bpm_fuente(conn, int(row["track_id"]), "manual", bpm)
            updated += 1

    conn.commit()
    conn.close()

    print(f"Imported {updated} manually entered BPMs.")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else None

    if modo == "export":
        export_csv()
    elif modo == "import":
        import_csv()
    else:
        print(__doc__)
