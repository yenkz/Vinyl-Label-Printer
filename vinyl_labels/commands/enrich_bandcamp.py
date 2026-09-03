"""
enrich_bandcamp.py — STEP 3

Bandcamp as a backup for anything still missing, designed for underground music
and small label releases (which are usually all on Bandcamp):

  - The record cover, if step 1 didn't get it from Discogs.
  - Track durations that Discogs had empty (very common for vinyl editions,
    where no one uploaded them).

Uses the public API of bandcamp.com's search engine (the same the site's search
uses, no key needed) to find the album, and from the album page extracts the
exact durations and cover. Bandcamp doesn't publish BPM or tonality: that's what
the other steps are for.

How to run it:
    python -m vinyl_labels bandcamp          # newly imported records only
    python -m vinyl_labels bandcamp --all    # retry the whole collection

Normal runs skip records already attempted, including old misses. Use --all
when you want to try incomplete records again.
"""

import argparse
import html
import json
import re
import time

import requests

from vinyl_labels.common import download_cover, format_duration, looks_similar
from vinyl_labels.db import get_connection, init_db, mark_workflow_step
from vinyl_labels.paths import PROJECT_ROOT

# Public API (no key) used by bandcamp.com's search engine.
BANDCAMP_SEARCH_API = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


class BandcampError(RuntimeError):
    """Bandcamp could not answer reliably; the operation should be retried."""


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels bandcamp",
        description="Fill missing covers and durations from Bandcamp."
    )
    parser.add_argument(
        "--all", action="store_true", help="retry the whole collection"
    )
    return parser.parse_args(arguments)


def search_album_bandcamp(artist, title):
    """Searches for the album on Bandcamp and returns the result (dict with
    item_url_path, name, band_name...) only if artist and title truly match,
    or None."""
    is_various = artist.lower().startswith("various")
    query = title if is_various else f"{artist} {title}"
    try:
        resp = requests.post(
            BANDCAMP_SEARCH_API,
            json={
                "search_text": query,
                "search_filter": "a",  # a = albums
                "full_page": False,
                "fan_id": None,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("auto", {}).get("results") or []
        if not isinstance(results, list):
            raise TypeError("results is not a list")
    except requests.RequestException as error:
        raise BandcampError(f"Bandcamp search request failed: {error}") from error
    except (ValueError, TypeError, AttributeError) as error:
        raise BandcampError("Bandcamp returned an invalid search response") from error

    for r in results:
        if not isinstance(r, dict):
            raise BandcampError("Bandcamp returned an invalid album result")
        if r.get("type") != "a" or not r.get("item_url_path"):
            continue
        if not looks_similar(r.get("name", ""), title):
            continue
        # Bandcamp's "band_name" is sometimes the label, not the artist;
        # so we check it, but only if it's not a compilation (where artist
        # isn't useful for comparison).
        if not is_various and not looks_similar(r.get("band_name", ""), artist, threshold=0.8):
            continue
        return r
    return None


def read_album_page(url):
    """Downloads the album page and returns (trackinfo, cover_url):
    the list of tracks with durations (from the data-tralbum JSON that
    Bandcamp embeds for its player) and the cover URL."""
    try:
        response = requests.get(url, headers=BROWSER, timeout=20)
        response.raise_for_status()
        page = response.text
    except requests.RequestException as error:
        raise BandcampError(f"Bandcamp album request failed: {error}") from error

    trackinfo = []
    data = re.search(r'data-tralbum="([^"]+)"', page)
    if data:
        try:
            trackinfo = json.loads(html.unescape(data.group(1))).get("trackinfo") or []
            if not isinstance(trackinfo, list):
                raise TypeError("trackinfo is not a list")
        except (ValueError, TypeError, AttributeError) as error:
            raise BandcampError("Bandcamp returned invalid album metadata") from error

    cover = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return trackinfo, cover.group(1) if cover else None


def main(arguments=None):
    args = parse_arguments(arguments)
    process_all = args.all

    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM releases WHERE ? OR NOT EXISTS ("
        " SELECT 1 FROM workflow_steps"
        " WHERE workflow_steps.release_id = releases.release_id"
        "   AND step = 'bandcamp')"
        " ORDER BY artist, title",
        (int(process_all),),
    )
    releases = cursor.fetchall()
    print(f"Records to check on Bandcamp: {len(releases)}\n")
    if not releases:
        conn.close()
        print("Nothing new to check. Use --all to revisit the whole collection.")
        return 0

    stats = {"covers": 0, "durations": 0, "no_bandcamp": 0}
    provider_failed = False
    for i, release in enumerate(releases, start=1):
        rid = release["release_id"]
        cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY sort_order, id",
            (rid,),
        )
        tracks_db = cursor.fetchall()

        missing_cover = not release["cover_path"] or not (PROJECT_ROOT / release["cover_path"]).exists()
        missing_durations = any(not t["duration_display"] for t in tracks_db)
        if not missing_cover and not missing_durations:
            mark_workflow_step(conn, rid, "bandcamp")
            conn.commit()
            continue  # already complete, don't waste requests

        label = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
        artist = release["artist"].split(" / ")[0]
        updates = []
        release_failed = False

        try:
            album = search_album_bandcamp(artist, release["title"])
        except BandcampError as error:
            provider_failed = True
            print(f"{label}: {error} (will retry)")
            continue

        if album:
            time.sleep(0.5)  # between search and page, no rush
            try:
                trackinfo, cover_url = read_album_page(album["item_url_path"])
            except BandcampError as error:
                provider_failed = True
                print(f"{label}: {error} (will retry)")
                continue

            if missing_durations and trackinfo:
                durations = 0
                for t in tracks_db:
                    if t["duration_display"]:
                        continue
                    match = next(
                        (b for b in trackinfo if b.get("duration") and looks_similar(b.get("title", ""), t["title"])),
                        None,
                    )
                    if match:
                        cursor.execute(
                            "UPDATE tracks SET duration_display = ? WHERE id = ?",
                            (format_duration(match["duration"]), t["id"]),
                        )
                        durations += 1
                stats["durations"] += durations
                if durations:
                    updates.append(f"{durations} durations")

            if missing_cover and cover_url:
                path = download_cover(cover_url, rid)
                if path:
                    cursor.execute("UPDATE releases SET cover_path = ? WHERE release_id = ?", (path, rid))
                    stats["covers"] += 1
                    updates.append("cover")
                else:
                    provider_failed = True
                    release_failed = True
                    updates.append("cover download failed (will retry)")
        else:
            stats["no_bandcamp"] += 1
            updates.append("not on Bandcamp")

        if not release_failed:
            mark_workflow_step(conn, rid, "bandcamp")
        conn.commit()
        print(f"{label}: {', '.join(updates) if updates else 'no updates'}")
        time.sleep(0.5)

    conn.close()

    print("\n" + "=" * 50)
    print(f"Covers downloaded: {stats['covers']} | durations completed: {stats['durations']}")
    if stats["no_bandcamp"]:
        print(f"Records not on Bandcamp: {stats['no_bandcamp']}.")
    print("Next step: python -m vinyl_labels spotify  (last backup, optional)")
    print("        or: python -m vinyl_labels analyze  (missing BPMs)")
    return 1 if provider_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
