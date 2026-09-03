"""
fetch_discogs.py — STEP 1

Connects to your Discogs account and imports NEW records from your collection
(all vinyl records, regardless of which folder in Discogs they're stored in)
and saves for each: artist, title, year, label, the list of tracks with their
position (A1, A2...), title and duration, and the cover of the actual vinyl
edition (Discogs is the master source: anything missing here is filled in later
by Beatport, Bandcamp, and Spotify).

How to run it:
    python -m vinyl_labels fetch          # new records only (default)
    python -m vinyl_labels fetch --all    # refresh the whole collection

You can run it as many times as you like: already-saved records are skipped,
new ones are added, and records you've removed from your collection are
deleted. Use --all when you deliberately want to refresh saved Discogs data;
your BPM, key, ISRC, and downloaded-audio data are preserved.

Note: Discogs limits requests to 60 per minute, so detailed imports take
roughly 1 second per new record (or per record with --all).
"""

import argparse
import difflib
import re
import time

import discogs_client
from discogs_client.exceptions import HTTPError

from vinyl_labels import config
from vinyl_labels import db as database
from vinyl_labels.common import download_cover, normalize
from vinyl_labels.db import get_connection, init_db
from vinyl_labels.paths import PROJECT_ROOT


def clean_artist(name):
    """Discogs adds suffixes like "Aphex Twin (2)" to distinguish artists
    with the same name. On a label that adds no value and breaks BPM search,
    so we remove it."""
    return re.sub(r"\s\(\d+\)$", "", name)


def same_track_identity(previous, title, artist):
    """Whether refreshed Discogs data still describes the same recording.

    Position alone is not an identity: Discogs edits sometimes rearrange a
    tracklist.  We retain user-entered/enriched data only when the title and
    per-track artist still match closely.  Small spelling corrections are
    accepted; a genuinely different song occupying the same position is not.
    """
    old_title = normalize(previous["title"])
    new_title = normalize(title)
    titles_match = bool(old_title and new_title) and (
        old_title == new_title
        or difflib.SequenceMatcher(None, old_title, new_title).ratio() >= 0.9
    )
    old_artist = normalize(previous["artist"])
    new_artist = normalize(artist)
    artists_match = old_artist == new_artist
    return titles_match and artists_match


def delete_track(cursor, track_id):
    """Deletes one track and every dependent row on legacy databases too."""
    for table in (
        "bpm_sources",
        "key_sources",
        "pending_downloads",
        "failed_downloads",
        "track_workflow_steps",
    ):
        cursor.execute(f"DELETE FROM {table} WHERE track_id = ?", (track_id,))
    cursor.execute("DELETE FROM tracks WHERE id = ?", (track_id,))


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


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels fetch",
        description="Import a Discogs vinyl collection.",
    )
    parser.add_argument("--all", action="store_true", help="refresh existing releases")
    return parser.parse_args(arguments)


def main(arguments=None):
    refresh_all = parse_arguments(arguments).all

    if not config.DISCOGS_USER_TOKEN:
        print(
            "Your Discogs token is missing.\n"
            "Copy .env.example as .env (if it doesn't exist) and fill in\n"
            "DISCOGS_USER_TOKEN with the token from\n"
            "https://www.discogs.com/settings/developers"
        )
        return 2

    init_db()  # create tables on first run
    if refresh_all and database.DB_PATH.exists():
        backup = database.backup_database()
        print(f"Safety backup created: {backup}")

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
    existing_ids = {
        row["release_id"] for row in cursor.execute("SELECT release_id FROM releases")
    }

    errors = []
    collection_ids = []
    imported = 0
    refreshed = 0
    skipped = 0
    traversed = 0

    for i, item in enumerate(all_folder.releases, start=1):
        traversed += 1
        fetched_details = False
        savepoint_open = False
        try:
            release = item.release
            collection_ids.append(release.id)
            if not refresh_all and release.id in existing_ids:
                skipped += 1
                continue

            # The Discogs request happens here (with retry
            # if we hit the rate limit).
            with_retry(release.refresh)
            fetched_details = True

            artist = (
                " / ".join(clean_artist(a.name) for a in release.artists)
                if release.artists
                else "Unknown"
            )

            action = "refreshing" if release.id in existing_ids else "new"
            print(f"[{i}/{total}] {artist} — {release.title} ({action})")

            # Record label and catalog number (for the label), plus the
            # vinyl release date (more precise than year alone, when
            # Discogs has it: "2024-12-30").
            label = clean_artist(release.labels[0].name) if release.labels else None
            catalog_number = release.labels[0].data.get("catno") if release.labels else None
            if catalog_number in ("none", ""):
                catalog_number = None
            release_date = release.data.get("released") or None

            # Resolve the complete incoming tracklist before changing the
            # database. Accessing a lazy Discogs object can itself fail; that
            # must leave the previously saved release completely untouched.
            incoming_tracks = []
            for track in release.tracklist:
                if not track.position:
                    continue
                track_artist = (
                    " / ".join(clean_artist(a.name) for a in track.artists)
                    if track.artists
                    else None
                )
                if track_artist == artist:
                    track_artist = None
                incoming_tracks.append(
                    {
                        "position": track.position,
                        "title": track.title,
                        "artist": track_artist,
                        "duration": track.duration or "",
                    }
                )

            if not incoming_tracks:
                raise ValueError(
                    "Discogs returned no positioned tracks; existing data was left untouched"
                )

            # One bad release must never leak half-applied changes into the
            # commit of the next successful release.
            cursor.execute("SAVEPOINT release_refresh")
            savepoint_open = True

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
            if not current_cover or not (PROJECT_ROOT / current_cover).exists():
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

            # Keep stable track IDs when Discogs still describes the same
            # recording. This preserves BPM/key/source/download relations
            # without detaching and recreating all dependent rows.
            cursor.execute(
                "SELECT * FROM tracks WHERE release_id = ?",
                (release.id,),
            )
            old_tracks = cursor.fetchall()
            previous_by_position = {row["position"]: row for row in old_tracks}
            retained_ids = set()

            for sort_order, track in enumerate(incoming_tracks):
                previous = previous_by_position.get(track["position"])
                if previous is not None and same_track_identity(
                    previous, track["title"], track["artist"]
                ):
                    duration = track["duration"] or previous["duration_display"] or ""
                    cursor.execute(
                        "UPDATE tracks SET position = ?, title = ?, artist = ?,"
                        " duration_display = ?, sort_order = ? WHERE id = ?",
                        (
                            track["position"], track["title"], track["artist"],
                            duration, sort_order, previous["id"],
                        ),
                    )
                    retained_ids.add(previous["id"])
                    continue

                # A different song now occupies this position. Never inherit
                # the old song's BPM, key, ISRC, or downloaded audio link.
                if previous is not None:
                    delete_track(cursor, previous["id"])
                cursor.execute(
                    "INSERT INTO tracks"
                    " (release_id, position, title, artist, duration_display, sort_order)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        release.id, track["position"], track["title"],
                        track["artist"], track["duration"], sort_order,
                    ),
                )

            # Positions removed from Discogs are genuinely gone. Explicitly
            # clear child rows for databases created before cascading FKs.
            for previous in old_tracks:
                if previous["id"] not in retained_ids:
                    delete_track(cursor, previous["id"])

            # A full Discogs refresh may have changed the track list. Make all
            # downstream steps pending for that release again. New releases
            # have no workflow rows yet, so they are already pending.
            if release.id in existing_ids:
                cursor.execute(
                    "DELETE FROM track_workflow_steps WHERE track_id IN "
                    "(SELECT id FROM tracks WHERE release_id = ?)",
                    (release.id,),
                )
                cursor.execute("DELETE FROM workflow_steps WHERE release_id = ?", (release.id,))
                refreshed += 1
            else:
                imported += 1
            cursor.execute("RELEASE SAVEPOINT release_refresh")
            savepoint_open = False

        except Exception as e:
            # If an individual record fails (e.g., a temporary network issue),
            # we note it and continue with the rest instead of stopping
            # the whole process.
            if savepoint_open:
                cursor.execute("ROLLBACK TO SAVEPOINT release_refresh")
                cursor.execute("RELEASE SAVEPOINT release_refresh")
                savepoint_open = False
            errors.append((getattr(item, "id", "?"), str(e)))
            print(f"   -> Error with this record, continuing: {e}")

        # Discogs allows 60 requests per minute. Only detailed refreshes cost
        # that request; skipped existing records need no pause.
        if fetched_details:
            time.sleep(1.1)

    # A paginated API can theoretically end cleanly before yielding its stated
    # count. Treat that as an operational failure: deleting based on a partial
    # view of the collection could remove valid local records.
    if traversed != total:
        message = f"Discogs returned {traversed} of {total} collection items"
        errors.append(("collection", message))
        print(f"\n   -> {message}; no local records were removed.")

    # Only a complete, error-free traversal is authoritative enough to remove
    # records that are no longer in the Discogs collection.
    if not errors and collection_ids:
        removed_ids = existing_ids - set(collection_ids)
        if removed_ids and not refresh_all:
            backup = database.backup_database()
            print(f"\nSafety backup created before collection removals: {backup}")
        placeholders = ",".join("?" * len(collection_ids))
        cursor.execute(
            f"DELETE FROM bpm_sources WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({placeholders}))",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM key_sources WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({placeholders}))",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM pending_downloads WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({placeholders}))",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM failed_downloads WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({placeholders}))",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM track_workflow_steps WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({placeholders}))",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM tracks WHERE release_id NOT IN ({placeholders})",
            collection_ids,
        )
        cursor.execute(
            f"DELETE FROM workflow_steps WHERE release_id NOT IN ({placeholders})",
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
    print(
        f"Done. {imported} new records imported, {refreshed} refreshed, "
        f"{skipped} existing records skipped."
    )
    if errors:
        print(f"{len(errors)} records had errors (see above).")
    print("Next step: python -m vinyl_labels beatport  (BPM and tonality)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
