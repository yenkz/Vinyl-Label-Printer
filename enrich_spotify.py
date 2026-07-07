"""
enrich_spotify.py — STEP 4 (optional)

Last in the chain of sources (Discogs → Beatport → Bandcamp → Spotify):
fills in from Spotify's API what still remains missing after previous steps:

  - The record cover, if neither Discogs nor Bandcamp had it.
  - Track durations that still are empty.
  - The ISRC code for each track (saved in the database as an identifier;
    not printed).

Note: Spotify apps created after Nov 2024 do NOT have BPM access
(the audio-features endpoint returns 403), so this doesn't replace
enrich_beatport.py / enrich_bpm.py.

How to run it:
    python enrich_spotify.py

You can run it as many times as you like: what's already enriched is skipped,
so subsequent runs are fast.
"""

import time
from pathlib import Path

import requests

import config
from common import download_cover, looks_similar
from db import get_connection, init_db

SPOTIFY_ACCOUNTS = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"


def get_spotify_token():
    """Requests an app token (client credentials). Lasts 1 hour,
    more than enough for the entire run."""
    resp = requests.post(
        SPOTIFY_ACCOUNTS,
        data={"grant_type": "client_credentials"},
        auth=(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_album_spotify(headers, artist, title):
    """Searches for the record on Spotify and returns the album (dict) only if
    artist and title truly match, or None."""
    # For compilations ("Various") the artist is useless for searching.
    is_various = artist.lower().startswith("various")
    query = title if is_various else f"{artist} {title}"
    try:
        resp = requests.get(
            f"{SPOTIFY_API}/search",
            params={"q": query, "type": "album", "limit": 5},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        candidates = resp.json().get("albums", {}).get("items") or []
    except requests.RequestException:
        return None

    for album in candidates:
        if not looks_similar(album["name"], title):
            continue
        if is_various:
            return album
        names = [a["name"] for a in album.get("artists", [])]
        if any(looks_similar(n, artist, umbral=0.8) for n in names):
            return album
    return None


def tracks_from_spotify_album(headers, album_id):
    """Fetches album tracks with duration and ISRC. The endpoint for
    multiple tracks at once (/tracks?ids=...) is blocked for new apps,
    so we have to request them one by one."""
    try:
        resp = requests.get(f"{SPOTIFY_API}/albums/{album_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        items = resp.json().get("tracks", {}).get("items") or []
    except requests.RequestException:
        return []

    tracks = []
    for item in items:
        try:
            resp = requests.get(f"{SPOTIFY_API}/tracks/{item['id']}", headers=headers, timeout=15)
            if resp.status_code != 200:
                continue
            t = resp.json()
        except requests.RequestException:
            continue
        seconds = (t.get("duration_ms") or 0) // 1000
        tracks.append(
            {
                "title": t.get("name") or "",
                "duration": f"{seconds // 60}:{seconds % 60:02d}" if seconds else "",
                "isrc": (t.get("external_ids") or {}).get("isrc"),
            }
        )
        time.sleep(0.2)
    return tracks


def main():
    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        print(
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET missing from .env.\n"
            "Create a free app at https://developer.spotify.com/dashboard\n"
            "and paste its credentials here. (This step is optional: without it,\n"
            "labels still work, just without cover or extra durations.)"
        )
        return

    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    token = get_spotify_token()
    headers = {"Authorization": f"Bearer {token}"}

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()
    print(f"Records in collection: {len(releases)}\n")

    stats = {"covers": 0, "durations": 0, "isrc": 0, "no_spotify": 0}
    for i, release in enumerate(releases, start=1):
        rid = release["release_id"]
        cursor.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (rid,))
        tracks_db = cursor.fetchall()

        missing_cover = not release["cover_path"] or not (Path(__file__).parent / release["cover_path"]).exists()
        missing_data = any(not t["duration_display"] or not t["isrc"] for t in tracks_db)
        if not missing_cover and not missing_data:
            continue  # already complete, don't waste requests

        label = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
        artist = release["artist"].split(" / ")[0]
        album = search_album_spotify(headers, artist, release["title"])
        updates = []

        if album:
            if missing_cover and album.get("images"):
                path = download_cover(album["images"][0]["url"], rid)
                if path:
                    cursor.execute("UPDATE releases SET cover_path = ? WHERE release_id = ?", (path, rid))
                    stats["covers"] += 1
                    updates.append("cover")

            if missing_data:
                spotify_tracks = tracks_from_spotify_album(headers, album["id"])
                durations = isrcs = 0
                for t in tracks_db:
                    match = next((s for s in spotify_tracks if looks_similar(s["title"], t["title"])), None)
                    if not match:
                        continue
                    if not t["duration_display"] and match["duration"]:
                        cursor.execute(
                            "UPDATE tracks SET duration_display = ? WHERE id = ?",
                            (match["duration"], t["id"]),
                        )
                        durations += 1
                    if not t["isrc"] and match["isrc"]:
                        cursor.execute("UPDATE tracks SET isrc = ? WHERE id = ?", (match["isrc"], t["id"]))
                        isrcs += 1
                stats["durations"] += durations
                stats["isrc"] += isrcs
                if durations:
                    updates.append(f"{durations} durations")
                if isrcs:
                    updates.append(f"{isrcs} ISRCs")
        else:
            stats["no_spotify"] += 1
            updates.append("not on Spotify")

        conn.commit()
        print(f"{label}: {', '.join(updates) if updates else 'no updates'}")
        time.sleep(0.2)

    conn.close()

    print("\n" + "=" * 50)
    print(
        f"Covers downloaded: {stats['covers']} | durations completed: {stats['durations']} | "
        f"ISRCs saved: {stats['isrc']}"
    )
    if stats["no_spotify"]:
        print(f"Records not on Spotify: {stats['no_spotify']} (normal with niche vinyl).")
    print("Next step: python enrich_bpm.py  (if there are pending BPMs)")
    print("        or: python render_labels.py")


if __name__ == "__main__":
    main()
