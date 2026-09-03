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
enrich_beatport.py / analyze_bpm.py.

How to run it:
    python -m vinyl_labels spotify          # newly imported records only
    python -m vinyl_labels spotify --all    # retry the whole collection

Normal runs skip records already attempted, including old misses. Use --all
when you want to try incomplete records again.
"""

import argparse
import time

import requests

from vinyl_labels import config
from vinyl_labels.common import download_cover, looks_similar
from vinyl_labels.db import get_connection, init_db, mark_workflow_step
from vinyl_labels.paths import PROJECT_ROOT

SPOTIFY_ACCOUNTS = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"


class SpotifyError(RuntimeError):
    """Spotify could not answer reliably; the operation should be retried."""


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels spotify",
        description="Fill missing cover, duration, and ISRC data from Spotify."
    )
    parser.add_argument(
        "--all", action="store_true", help="retry the whole collection"
    )
    return parser.parse_args(arguments)


def get_spotify_token():
    """Requests an app token (client credentials). Lasts 1 hour,
    more than enough for the entire run."""
    try:
        resp = requests.post(
            SPOTIFY_ACCOUNTS,
            data={"grant_type": "client_credentials"},
            auth=(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET),
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
    except requests.RequestException as error:
        raise SpotifyError(f"Spotify authentication failed: {error}") from error
    except (KeyError, ValueError, TypeError) as error:
        raise SpotifyError("Spotify returned an invalid authentication response") from error
    if not token:
        raise SpotifyError("Spotify returned an empty authentication token")
    return token


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
        resp.raise_for_status()
        candidates = resp.json().get("albums", {}).get("items") or []
        if not isinstance(candidates, list):
            raise TypeError("album items is not a list")
    except requests.RequestException as error:
        raise SpotifyError(f"Spotify album search failed: {error}") from error
    except (ValueError, TypeError, AttributeError) as error:
        raise SpotifyError("Spotify returned an invalid album search response") from error

    for album in candidates:
        if (
            not isinstance(album, dict)
            or not album.get("id")
            or not isinstance(album.get("name"), str)
        ):
            raise SpotifyError("Spotify returned an invalid album result")
        if not looks_similar(album["name"], title):
            continue
        if is_various:
            return album
        artists = album.get("artists") or []
        if not isinstance(artists, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in artists
        ):
            raise SpotifyError("Spotify returned invalid album artists")
        names = [item["name"] for item in artists]
        if any(looks_similar(n, artist, threshold=0.8) for n in names):
            return album
    return None


def tracks_from_spotify_album(headers, album_id):
    """Fetches album tracks with duration and ISRC. The endpoint for
    multiple tracks at once (/tracks?ids=...) is blocked for new apps,
    so we have to request them one by one."""
    try:
        resp = requests.get(f"{SPOTIFY_API}/albums/{album_id}", headers=headers, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("tracks", {}).get("items") or []
        if not isinstance(items, list):
            raise TypeError("track items is not a list")
    except requests.RequestException as error:
        raise SpotifyError(f"Spotify album request failed: {error}") from error
    except (ValueError, TypeError, AttributeError) as error:
        raise SpotifyError("Spotify returned invalid album metadata") from error

    tracks = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            raise SpotifyError("Spotify returned an invalid track result")
        try:
            resp = requests.get(f"{SPOTIFY_API}/tracks/{item['id']}", headers=headers, timeout=15)
            resp.raise_for_status()
            t = resp.json()
            if not isinstance(t, dict):
                raise TypeError("track is not an object")
        except requests.RequestException as error:
            raise SpotifyError(f"Spotify track request failed: {error}") from error
        except (ValueError, TypeError, AttributeError) as error:
            raise SpotifyError("Spotify returned invalid track metadata") from error
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
        "   AND step = 'spotify')"
        " ORDER BY artist, title",
        (int(process_all),),
    )
    releases = cursor.fetchall()
    print(f"Records to check on Spotify: {len(releases)}\n")
    if not releases:
        conn.close()
        print("Nothing new to check. Use --all to revisit the whole collection.")
        return 0

    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        conn.close()
        print(
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET missing from .env.\n"
            "Create a free app at https://developer.spotify.com/dashboard\n"
            "and paste its credentials here. (This step is optional: without it,\n"
            "labels still work, just without cover or extra durations.)"
        )
        return 0

    try:
        token = get_spotify_token()
    except SpotifyError as error:
        conn.close()
        print(f"Could not authenticate with Spotify: {error}")
        return 1
    headers = {"Authorization": f"Bearer {token}"}

    stats = {"covers": 0, "durations": 0, "isrc": 0, "no_spotify": 0}
    provider_failed = False
    for i, release in enumerate(releases, start=1):
        rid = release["release_id"]
        cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY sort_order, id",
            (rid,),
        )
        tracks_db = cursor.fetchall()

        missing_cover = not release["cover_path"] or not (PROJECT_ROOT / release["cover_path"]).exists()
        missing_data = any(not t["duration_display"] or not t["isrc"] for t in tracks_db)
        if not missing_cover and not missing_data:
            mark_workflow_step(conn, rid, "spotify")
            conn.commit()
            continue  # already complete, don't waste requests

        label = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
        artist = release["artist"].split(" / ")[0]
        updates = []
        release_failed = False

        try:
            album = search_album_spotify(headers, artist, release["title"])
        except SpotifyError as error:
            provider_failed = True
            print(f"{label}: {error} (will retry)")
            continue

        if album:
            if missing_cover and album.get("images"):
                path = download_cover(album["images"][0]["url"], rid)
                if path:
                    cursor.execute("UPDATE releases SET cover_path = ? WHERE release_id = ?", (path, rid))
                    stats["covers"] += 1
                    updates.append("cover")
                else:
                    provider_failed = True
                    release_failed = True
                    updates.append("cover download failed (will retry)")

            if missing_data:
                try:
                    spotify_tracks = tracks_from_spotify_album(headers, album["id"])
                except SpotifyError as error:
                    provider_failed = True
                    release_failed = True
                    updates.append(f"{error} (will retry)")
                    spotify_tracks = []
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

        if not release_failed:
            mark_workflow_step(conn, rid, "spotify")
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
    print("Next step: python -m vinyl_labels analyze  (if BPMs are pending)")
    print("        or: python -m vinyl_labels render")
    return 1 if provider_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
