"""
fetch_discogs.py — STEP 1

Connects to your Discogs account and imports ALL records from your collection
(all vinyl records, regardless of which folder in Discogs they're stored in)
and saves for each: artist, title, year, label, the list of tracks with their
position (A1, A2...), title and duration, and the cover of the actual vinyl
edition (Discogs is the master source: anything missing here is filled in later
by Beatport, Bandcamp, and Spotify).

How to run it:
    python fetch_discogs.py

You can run it as many times as you like: already-saved records are updated
(without losing any BPM data you've entered), new ones are added, and records
you've removed from your collection are deleted.

Note: Discogs limits requests to 60 per minute, so with a large collection
this step takes a while — roughly 1 second per record.
"""

import re
import time
from pathlib import Path

import discogs_client
from discogs_client.exceptions import HTTPError

import config
from common import download_cover
from db import get_connection, init_db


def clean_artist(name):
    """Discogs adds suffixes like "Aphex Twin (2)" to distinguish artists
    with the same name. On a label that adds no value and breaks BPM search,
    so we remove it."""
    return re.sub(r"\s\(\d+\)$", "", name)


def with_retry(function, attempts=3):
    """Executes a Discogs API call; if it responds 429 (too many requests),
    waits as requested by the server and retries."""
    for attempt in range(attempts):
        try:
            return function()
        except HTTPError as e:
            if e.status_code == 429 and attempt < attempts - 1:
                wait = 60
                print(f"   Discogs asking to wait... pausing {wait}s and resuming.")
                time.sleep(wait)
            else:
                raise


def main():
    if not config.DISCOGS_USER_TOKEN:
        print(
            "Your Discogs token is missing.\n"
            "Copy .env.example as .env (if it doesn't exist) and fill in\n"
            "DISCOGS_USER_TOKEN with the token from\n"
            "https://www.discogs.com/settings/developers"
        )
        return

    init_db()  # create tables on first run

    print("Connecting to Discogs...")
    d = discogs_client.Client(
        config.DISCOGS_USER_AGENT,
        user_token=config.DISCOGS_USER_TOKEN,
    )

    me = d.identity()
    print(f"Connected as: {me.username}\n")

    # Folder 0 ("All") always contains your entire collection,
    # regardless of how you've organized it into subfolders.
    all_folder = me.collection_folders[0]
    total = all_folder.count
    print(f"Records found in your collection: {total}\n")

    conn = get_connection()
    cursor = conn.cursor()

    errors = []
    collection_ids = []

    for i, item in enumerate(all_folder.releases, start=1):
        try:
            release = item.release
            # The Discogs request happens here (with retry
            # if we hit the rate limit).
            with_retry(release.refresh)
            collection_ids.append(release.id)

            artist = (
                " / ".join(clean_artist(a.name) for a in release.artists)
                if release.artists
                else "Unknown"
            )

            print(f"[{i}/{total}] {artist} — {release.title}")

            # Record label and catalog number (for the label), plus the
            # vinyl release date (more precise than year alone, when
            # Discogs has it: "2024-12-30").
            label = clean_artist(release.labels[0].name) if release.labels else None
            catalog_number = release.labels[0].data.get("catno") if release.labels else None
            if catalog_number in ("none", ""):
                catalog_number = None
            release_date = release.data.get("released") or None

            cursor.execute(
                """
                INSERT INTO releases (release_id, artist, title, year, label, catno, released)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_id) DO UPDATE SET
                    artist = excluded.artist,
                    title = excluded.title,
                    year = excluded.year,
                    label = excluded.label,
                    catno = excluded.catno,
                    released = excluded.released
                """,
                (release.id, artist, release.title, release.year or None, label, catalog_number, release_date),
            )

            # The cover, directly from Discogs (the master source: it's the
            # photo of the actual vinyl edition). The release request already
            # includes image URLs, so this doesn't cost extra API calls. If a
            # cover was already downloaded, we don't overwrite it (to redo one:
            # delete it from covers/).
            cursor.execute("SELECT cover_path FROM releases WHERE release_id = ?", (release.id,))
            current_cover = cursor.fetchone()["cover_path"]
            if not current_cover or not (Path(__file__).parent / current_cover).exists():
                images = release.data.get("images") or []
                primary = [im for im in images if im.get("type") == "primary"] or images
                if primary and primary[0].get("uri"):
                    path = download_cover(primary[0]["uri"], release.id)
                    if path:
                        cursor.execute(
                            "UPDATE releases SET cover_path = ? WHERE release_id = ?",
                            (path, release.id),
                        )
                        print("   cover downloaded from Discogs")

            # Before replacing tracks, save what they already had and isn't
            # from Discogs (BPM and its validation status, key, ISRC,
            # durations filled in from other sources) so we don't lose it
            # each time you update the collection.
            cursor.execute(
                "SELECT position, bpm, bpm_source, bpm_alt, bpm_needs_review, bpm_verified,"
                "       key, key_source, isrc, duration_display"
                " FROM tracks WHERE release_id = ?",
                (release.id,),
            )
            previous_tracks = {row["position"]: dict(row) for row in cursor.fetchall()}

            # Same with the source details (bpm_sources): since tracks get
            # recreated with new IDs, we save them by position and reattach
            # them to the new ID when inserting.
            cursor.execute(
                "SELECT tracks.position, bpm_sources.source, bpm_sources.bpm, bpm_sources.detail"
                " FROM bpm_sources JOIN tracks ON tracks.id = bpm_sources.track_id"
                " WHERE tracks.release_id = ?",
                (release.id,),
            )
            previous_sources = {}
            for row in cursor.fetchall():
                previous_sources.setdefault(row["position"], []).append(
                    (row["source"], row["bpm"], row["detail"])
                )

            cursor.execute(
                "DELETE FROM bpm_sources WHERE track_id IN"
                " (SELECT id FROM tracks WHERE release_id = ?)",
                (release.id,),
            )
            cursor.execute("DELETE FROM tracks WHERE release_id = ?", (release.id,))

            for track in release.tracklist:
                # Rows without position are section titles
                # ("Side A", suite names, etc.), not songs.
                if not track.position:
                    continue
                previous = previous_tracks.get(track.position) or {}
                # If Discogs doesn't have the duration but we already filled
                # it from another source, we keep it.
                duration = track.duration or previous.get("duration_display") or ""

                # On "Various" records (compilations), Discogs stores the
                # actual artist per track (track.artists) instead of at the
                # record level. If the track has one, we save it separately;
                # if not (single-artist record), it remains None and we use
                # the record artist.
                track_artist = (
                    " / ".join(clean_artist(a.name) for a in track.artists)
                    if track.artists
                    else None
                )
                if track_artist == artist:
                    track_artist = None

                cursor.execute(
                    """
                    INSERT INTO tracks (release_id, position, title, artist, duration_display,
                                        bpm, bpm_source, bpm_alt, bpm_needs_review, bpm_verified,
                                        key, key_source, isrc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        release.id, track.position, track.title, track_artist, duration,
                        previous.get("bpm"), previous.get("bpm_source"), previous.get("bpm_alt"),
                        previous.get("bpm_needs_review") or 0, previous.get("bpm_verified") or 0,
                        previous.get("key"), previous.get("key_source"), previous.get("isrc"),
                    ),
                )
                new_track_id = cursor.lastrowid
                for source, bpm, detail in previous_sources.get(track.position, []):
                    cursor.execute(
                        "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm, detail)"
                        " VALUES (?, ?, ?, ?)",
                        (new_track_id, source, bpm, detail),
                    )

            conn.commit()

        except Exception as e:
            # If an individual record fails (e.g., a temporary network issue),
            # we note it and continue with the rest instead of stopping
            # the whole process.
            errors.append((getattr(item, "id", "?"), str(e)))
            print(f"   -> Error with this record, continuing: {e}")

        # Discogs allows 60 requests per minute. This pause prevents
        # temporary blocking if you have a large collection.
        time.sleep(1.1)

    # If the traversal completed without errors, we delete records that
    # are no longer in your Discogs collection (you sold them, etc.).
    # If there were errors, we don't delete anything just in case.
    if not errors and collection_ids:
        placeholders = ",".join("?" * len(collection_ids))
        cursor.execute(
            f"DELETE FROM bpm_sources WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({placeholders}))",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM tracks WHERE release_id NOT IN ({placeholders})",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM releases WHERE release_id NOT IN ({placeholders})",
            collection_ids,
        )
        if cursor.rowcount:
            print(f"\nRemoved {cursor.rowcount} records no longer in your collection.")
        conn.commit()

    conn.close()

    print("\n" + "=" * 50)
    print(f"Done. {len(collection_ids)} records saved successfully.")
    if errors:
        print(f"{len(errors)} records had errors (see above).")
    print("Next step: python enrich_beatport.py  (BPM and tonality)")


if __name__ == "__main__":
    main()
