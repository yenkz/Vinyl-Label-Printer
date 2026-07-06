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
    python enrich_bandcamp.py

You can run it as many times as you like: records that are already complete
are skipped without making requests.
"""

import html
import json
import re
import time
from pathlib import Path

import requests

from comunes import bajar_tapa, formatear_duracion, se_parecen
from db import get_connection, init_db

# Public API (no key) used by bandcamp.com's search engine.
BANDCAMP_SEARCH_API = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


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
    except (requests.RequestException, ValueError):
        return None

    for r in results:
        if r.get("type") != "a" or not r.get("item_url_path"):
            continue
        if not se_parecen(r.get("name", ""), title):
            continue
        # Bandcamp's "band_name" is sometimes the label, not the artist;
        # so we check it, but only if it's not a compilation (where artist
        # isn't useful for comparison).
        if not is_various and not se_parecen(r.get("band_name", ""), artist, umbral=0.8):
            continue
        return r
    return None


def read_album_page(url):
    """Downloads the album page and returns (trackinfo, cover_url):
    the list of tracks with durations (from the data-tralbum JSON that
    Bandcamp embeds for its player) and the cover URL."""
    try:
        page = requests.get(url, headers=BROWSER, timeout=20).text
    except requests.RequestException:
        return [], None

    trackinfo = []
    data = re.search(r'data-tralbum="([^"]+)"', page)
    if data:
        try:
            trackinfo = json.loads(html.unescape(data.group(1))).get("trackinfo") or []
        except ValueError:
            pass

    cover = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return trackinfo, cover.group(1) if cover else None


def main():
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()
    print(f"Records in collection: {len(releases)}\n")

    stats = {"covers": 0, "durations": 0, "no_bandcamp": 0}
    for i, release in enumerate(releases, start=1):
        rid = release["release_id"]
        cursor.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (rid,))
        tracks_db = cursor.fetchall()

        missing_cover = not release["cover_path"] or not (Path(__file__).parent / release["cover_path"]).exists()
        missing_durations = any(not t["duration_display"] for t in tracks_db)
        if not missing_cover and not missing_durations:
            continue  # already complete, don't waste requests

        label = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
        artist = release["artist"].split(" / ")[0]
        album = search_album_bandcamp(artist, release["title"])
        updates = []

        if album:
            time.sleep(0.5)  # between search and page, no rush
            trackinfo, cover_url = read_album_page(album["item_url_path"])

            if missing_durations and trackinfo:
                durations = 0
                for t in tracks_db:
                    if t["duration_display"]:
                        continue
                    match = next(
                        (b for b in trackinfo if b.get("duration") and se_parecen(b.get("title", ""), t["title"])),
                        None,
                    )
                    if match:
                        cursor.execute(
                            "UPDATE tracks SET duration_display = ? WHERE id = ?",
                            (formatear_duracion(match["duration"]), t["id"]),
                        )
                        durations += 1
                stats["durations"] += durations
                if durations:
                    updates.append(f"{durations} durations")

            if missing_cover and cover_url:
                path = bajar_tapa(cover_url, rid)
                if path:
                    cursor.execute("UPDATE releases SET cover_path = ? WHERE release_id = ?", (path, rid))
                    stats["covers"] += 1
                    updates.append("cover")
        else:
            stats["no_bandcamp"] += 1
            updates.append("not on Bandcamp")

        conn.commit()
        print(f"{label}: {', '.join(updates) if updates else 'no updates'}")
        time.sleep(0.5)

    conn.close()

    print("\n" + "=" * 50)
    print(f"Covers downloaded: {stats['covers']} | durations completed: {stats['durations']}")
    if stats["no_bandcamp"]:
        print(f"Records not on Bandcamp: {stats['no_bandcamp']}.")
    print("Next step: python enrich_spotify.py  (last backup, optional)")
    print("        or: python enrich_bpm.py      (missing BPMs)")


if __name__ == "__main__":
    main()
