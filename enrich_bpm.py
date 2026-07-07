"""
enrich_bpm.py — STEP 6 (optional, last resort)

For each track that still has no BPM (because Beatport didn't have it and
audio measurement couldn't either), searches for it automatically:

  1. First on Deezer, which is free and requires no API key.
  2. If not found and GETSONGBPM_API_KEY is configured in config.py,
     also tries getsongbpm.com.

This is "best effort": rare vinyl, niche editions, remixes, or unnamed
instrumental tracks often don't appear. What is found is noted as a source
in bpm_sources but NOT validated: you put the checkmark in the editor
(edit_bpm.py), which shows all sources side by side. What isn't found
you enter manually there.

How to run it:
    python enrich_bpm.py
"""

import time
import urllib.parse

import requests

import config
from db import get_connection, init_db, record_bpm_source

DEEZER_API = "https://api.deezer.com"
GETSONGBPM_API = "https://api.getsong.co"


def search_bpm_deezer(title, artist):
    """
    Searches for a track on Deezer by title + artist and returns its BPM
    (float) or None. Deezer requires no API key, but BPM only comes in the
    track details, so it takes two requests: search then get the first
    result's details.

    Deezer's "strict" search skips several tracks that simple search finds,
    so we try both. In simple search we check that the artist matches,
    to not get the BPM of another song with the same name.
    """
    try:
        track_id = None
        # 1) Strict search by exact artist + title.
        resp = requests.get(
            f"{DEEZER_API}/search",
            params={"q": f'artist:"{artist}" track:"{title}"', "limit": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("data") or []
            if results:
                track_id = results[0]["id"]

        # 2) If that didn't work, simple search, verifying the artist.
        if track_id is None:
            time.sleep(0.3)  # Deezer allows ~50 requests per 5 seconds
            resp = requests.get(
                f"{DEEZER_API}/search",
                params={"q": f"{artist} {title}", "limit": 5},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            for result in resp.json().get("data") or []:
                if artist.lower() in result["artist"]["name"].lower():
                    track_id = result["id"]
                    break
            if track_id is None:
                return None

        time.sleep(0.3)

        resp = requests.get(f"{DEEZER_API}/track/{track_id}", timeout=10)
        if resp.status_code != 200:
            return None
        bpm = resp.json().get("bpm")
    except (requests.RequestException, ValueError, KeyError):
        return None

    # Deezer returns 0 when it hasn't analyzed the track.
    if not bpm:
        return None
    return float(bpm)


def search_bpm_getsongbpm(title, artist):
    """
    Searches for a track on getsongbpm.com by title + artist.
    Returns BPM (float) if found, None otherwise.
    """
    lookup = f"song:{title} artist:{artist}"
    params = {
        "api_key": config.GETSONGBPM_API_KEY,
        "type": "both",
        "lookup": lookup,
        "limit": 1,
    }
    url = f"{GETSONGBPM_API}/search/?{urllib.parse.urlencode(params)}"

    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None
    results = data.get("search") or []
    # getsongbpm.com sometimes returns a single result as a dict instead
    # of a list with one element, and "error" (string) when no match.
    if isinstance(results, dict):
        results = [results]
    if not results or not isinstance(results, list):
        return None

    tempo = results[0].get("tempo")
    try:
        return float(tempo)
    except (TypeError, ValueError):
        return None


def main():
    init_db()  # just in case you haven't run any other step yet
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT tracks.id, tracks.title,
               COALESCE(tracks.artist, releases.artist) AS artist
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm IS NULL
        """
    )
    pending = cursor.fetchall()

    print(f"Tracks without BPM: {len(pending)}\n")
    if not config.GETSONGBPM_API_KEY:
        print(
            "(GETSONGBPM_API_KEY not configured: searching Deezer only.\n"
            " It's optional, Deezer usually is enough.)\n"
        )

    found = {"deezer": 0, "getsongbpm": 0}
    for i, row in enumerate(pending, start=1):
        # If the record has multiple artists we store them as
        # "Artist 1 / Artist 2"; for searching we use only the first.
        artist = row["artist"].split(" / ")[0]

        bpm = search_bpm_deezer(row["title"], artist)
        source = "deezer"

        if not bpm and config.GETSONGBPM_API_KEY:
            bpm = search_bpm_getsongbpm(row["title"], artist)
            source = "getsongbpm"

        if bpm:
            cursor.execute(
                "UPDATE tracks SET bpm = ?, bpm_source = ? WHERE id = ?",
                (bpm, source, row["id"]),
            )
            record_bpm_source(conn, row["id"], source, bpm)
            conn.commit()
            found[source] += 1
            print(f"[{i}/{len(pending)}] OK  {row['artist']} - {row['title']} -> {bpm:g} BPM ({source})")
        else:
            print(f"[{i}/{len(pending)}] --  {row['artist']} - {row['title']} (not found)")

        time.sleep(0.3)

    conn.close()

    total_ok = sum(found.values())
    print("\n" + "=" * 50)
    print(f"BPM found automatically for {total_ok} of {len(pending)} tracks")
    print(f"(Deezer: {found['deezer']}, getsongbpm: {found['getsongbpm']}).")
    print("Nothing validates itself: confirm them in the editor, where you")
    print("see all sources for each track: python edit_bpm.py")


if __name__ == "__main__":
    main()
