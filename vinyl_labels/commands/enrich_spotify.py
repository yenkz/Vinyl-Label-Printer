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

The release is matched first because it is the safest identity check. Any track
whose duration is still missing then gets a strict track-level search, allowing
Spotify singles and compilation appearances to fill gaps without requiring the
whole EP to exist there.

Normal runs skip records already attempted, including old misses. Use --all
when you want to try incomplete records again.
"""

import argparse
import re
import time

import requests

from vinyl_labels import config
from vinyl_labels.common import download_cover, format_duration, looks_similar, normalize
from vinyl_labels.db import get_connection, init_db, mark_workflow_step
from vinyl_labels.paths import PROJECT_ROOT

SPOTIFY_ACCOUNTS = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
TRACK_SEARCH_LIMIT = 10
AMBIGUOUS_DURATION_SECONDS = 2

PLACEHOLDER_ARTISTS = {"", "unknown", "various", "variousartists"}
GENERIC_VERSION_SUFFIXES = ("originalmix", "originalversion")


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
            params={
                "q": query,
                "type": "album",
                "market": config.SPOTIFY_MARKET,
                "limit": 5,
            },
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


def _comparable_track_title(title):
    """Normalize a title without erasing meaningful remix/edit information."""
    value = normalize(title)
    for suffix in GENERIC_VERSION_SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _track_titles_match(left, right):
    left = _comparable_track_title(left)
    right = _comparable_track_title(right)
    return bool(left and right and left == right)


def _credited_artists(artist):
    return [
        name.strip()
        for name in re.split(r"\s*(?:/|&|,|\bfeat\.?\b|\bfeaturing\b)\s*", artist or "")
        if normalize(name) not in PLACEHOLDER_ARTISTS
    ]


def _artists_match(expected, candidate_names):
    expected_names = {normalize(name) for name in _credited_artists(expected)}
    if not expected_names:
        return False
    actual_names = {normalize(name) for name in candidate_names if normalize(name)}
    return bool(expected_names & actual_names)


def _spotify_track(item):
    """Validate and reduce a Spotify TrackObject to fields used by this command."""
    if not isinstance(item, dict) or not item.get("id"):
        raise SpotifyError("Spotify returned an invalid track result")
    title = item.get("name")
    artists = item.get("artists") or []
    album = item.get("album") or {}
    if (
        not isinstance(title, str)
        or not isinstance(artists, list)
        or any(not isinstance(a, dict) or not isinstance(a.get("name"), str) for a in artists)
        or not isinstance(album, dict)
    ):
        raise SpotifyError("Spotify returned invalid track metadata")
    duration_ms = item.get("duration_ms") or 0
    if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
        raise SpotifyError("Spotify returned an invalid track duration")
    external_ids = item.get("external_ids") or {}
    if not isinstance(external_ids, dict):
        raise SpotifyError("Spotify returned invalid track identifiers")
    return {
        "id": item["id"],
        "title": title,
        "artists": [a["name"] for a in artists],
        "album": album.get("name") if isinstance(album.get("name"), str) else "",
        "duration_seconds": round(duration_ms / 1000) if duration_ms else 0,
        "isrc": external_ids.get("isrc"),
    }


def choose_spotify_track(
    candidates,
    artist,
    title,
    release_title="",
    isrc=None,
    *,
    release_already_matched=False,
):
    """Return one unambiguous recording, or None rather than guess.

    Exact ISRC wins when one is already known. Otherwise title and credited
    artist must match exactly after punctuation folding. A matching release is
    preferred; duplicate Spotify appearances are accepted only when their
    durations agree within two seconds.
    """
    normalized_isrc = normalize(isrc)
    viable = []
    for candidate in candidates:
        if normalized_isrc:
            if normalize(candidate.get("isrc")) == normalized_isrc:
                viable.append(candidate)
            continue
        if not _track_titles_match(candidate["title"], title):
            continue
        if not release_already_matched and not _artists_match(artist, candidate["artists"]):
            continue
        viable.append(candidate)

    if not viable:
        return None
    if normalized_isrc:
        return viable[0]

    release_matches = [
        candidate
        for candidate in viable
        if release_title
        and candidate.get("album")
        and looks_similar(candidate["album"], release_title, threshold=0.85)
    ]
    if release_matches:
        viable = release_matches
    if len(viable) == 1:
        return viable[0]

    durations = [candidate["duration_seconds"] for candidate in viable if candidate["duration_seconds"]]
    if durations and max(durations) - min(durations) <= AMBIGUOUS_DURATION_SECONDS:
        return viable[0]
    return None


def search_track_spotify(headers, artist, title, release_title="", isrc=None):
    """Search one recording and return a strict, unambiguous Spotify match."""
    credited_artists = _credited_artists(artist)
    primary_artist = credited_artists[0] if credited_artists else ""
    if not isrc and not primary_artist:
        return None
    query = f"isrc:{isrc}" if isrc else f"track:{title} artist:{primary_artist}"
    try:
        resp = requests.get(
            f"{SPOTIFY_API}/search",
            params={
                "q": query,
                "type": "track",
                "market": config.SPOTIFY_MARKET,
                "limit": TRACK_SEARCH_LIMIT,
            },
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("tracks", {}).get("items") or []
        if not isinstance(items, list):
            raise TypeError("track items is not a list")
        candidates = [_spotify_track(item) for item in items]
    except requests.RequestException as error:
        raise SpotifyError(f"Spotify track search failed: {error}") from error
    except (ValueError, TypeError, AttributeError) as error:
        raise SpotifyError("Spotify returned an invalid track search response") from error
    return choose_spotify_track(candidates, artist, title, release_title, isrc)


def tracks_from_spotify_album(headers, album_id):
    """Fetches album tracks with duration and ISRC. The endpoint for
    multiple tracks at once (/tracks?ids=...) is blocked for new apps,
    so we have to request them one by one."""
    try:
        resp = requests.get(
            f"{SPOTIFY_API}/albums/{album_id}",
            params={"market": config.SPOTIFY_MARKET},
            headers=headers,
            timeout=15,
        )
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
            resp = requests.get(
                f"{SPOTIFY_API}/tracks/{item['id']}",
                params={"market": config.SPOTIFY_MARKET},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            t = resp.json()
            if not isinstance(t, dict):
                raise TypeError("track is not an object")
        except requests.RequestException as error:
            raise SpotifyError(f"Spotify track request failed: {error}") from error
        except (ValueError, TypeError, AttributeError) as error:
            raise SpotifyError("Spotify returned invalid track metadata") from error
        tracks.append(_spotify_track(t))
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

    stats = {
        "covers": 0,
        "durations": 0,
        "isrc": 0,
        "no_album": 0,
        "track_matches": 0,
    }
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
                    track_artist = t["artist"] or release["artist"]
                    match = choose_spotify_track(
                        spotify_tracks,
                        track_artist,
                        t["title"],
                        release["title"],
                        t["isrc"],
                        release_already_matched=True,
                    )
                    if not match:
                        continue
                    if not t["duration_display"] and match["duration_seconds"]:
                        cursor.execute(
                            "UPDATE tracks SET duration_display = ? WHERE id = ?",
                            (format_duration(match["duration_seconds"]), t["id"]),
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
            stats["no_album"] += 1
            updates.append("album not on Spotify")

        # The EP/album may not be on Spotify even when individual tracks are
        # available as singles or on compilations. Search only durations that
        # remain unresolved; never replace Discogs or Bandcamp data.
        unresolved = cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ?"
            " AND (duration_display IS NULL OR TRIM(duration_display) = '')"
            " ORDER BY sort_order, id",
            (rid,),
        ).fetchall()
        release_track_matches = 0
        for track in unresolved:
            track_artist = track["artist"] or release["artist"]
            try:
                match = search_track_spotify(
                    headers,
                    track_artist,
                    track["title"],
                    release["title"],
                    track["isrc"],
                )
            except SpotifyError as error:
                provider_failed = True
                release_failed = True
                updates.append(f"track search failed: {error} (will retry)")
                break
            if match and match["duration_seconds"]:
                cursor.execute(
                    "UPDATE tracks SET duration_display = ?,"
                    " isrc = CASE WHEN isrc IS NULL OR TRIM(isrc) = ''"
                    " THEN ? ELSE isrc END WHERE id = ?",
                    (
                        format_duration(match["duration_seconds"]),
                        match["isrc"],
                        track["id"],
                    ),
                )
                stats["durations"] += 1
                stats["track_matches"] += 1
                release_track_matches += 1
                if not track["isrc"] and match["isrc"]:
                    stats["isrc"] += 1
            time.sleep(0.2)

        if release_track_matches:
            updates.append(f"{release_track_matches} durations from track search")

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
    if stats["track_matches"]:
        print(f"Durations found by individual track search: {stats['track_matches']}.")
    if stats["no_album"]:
        print(f"Records without a Spotify album match: {stats['no_album']} (individual tracks checked).")
    print("Next step: python -m vinyl_labels analyze  (if BPMs are pending)")
    print("        or: python -m vinyl_labels render")
    return 1 if provider_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
