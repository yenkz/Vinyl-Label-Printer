"""
enrich_beatport.py — STEP 2

Searches ALL tracks on Beatport, the metadata reference for electronic music
(not just tracks without BPM: even if a track already has a measured BPM,
Beatport's official data is consulted anyway), and saves what it finds:

  - The official BPM (from the track's sheet, uploaded by the label).
    It's noted as a source in bpm_sources and becomes the track's main BPM —
    unless you've already entered or validated one manually, which always wins.
  - The tonality (key), displayed on the label in Camelot notation ("8A").
  - The ISRC, if not already present.

It also cross-references sources: if a track already had automatic BPM (from
audio measurement or Deezer) and Beatport says the same, the question is
resolved — but NOTHING validates itself: you put the green checkmark only
in the editor (python edit_bpm.py), where you see all sources side by side.
If they differ, the track is marked as doubtful, with the other value one click away.

How does it work without an API key? Beatport doesn't give public API access,
but its own embedded player (embed.beatport.com) uses an "anonymous client"
whose credentials are public: they travel in the player's JavaScript to any
browser. This script does the same as the player: it requests an anonymous token
with those credentials and queries the official API (api.beatport.com/v4).
If Beatport rotates the credentials, they're automatically extracted again from
the embed's JavaScript.

For not importing tracks from different songs, the candidate must match the
artist, title (including remix/mix name), and duration according to Discogs.

How to run it:
    python enrich_beatport.py        # all pending tracks
    python enrich_beatport.py 5      # only 5 (for testing)
"""

import re
import sys
import time

import requests

from common import (
    to_camelot,
    fit_to_range,
    normalize,
    normalize_key,
    parse_duration,
    looks_similar,
)
from db import get_connection, init_db, record_bpm_source

BEATPORT_API = "https://api.beatport.com/v4"
BEATPORT_EMBED = "https://embed.beatport.com/"
BEATPORT_TOKEN_URL = "https://account.beatport.com/o/token/"

# Credentials for the anonymous client of the embedded player. They're public
# by design (any browser receives them when opening a Beatport embed) and only
# provide anonymous read-only access to the catalog. If they stop working,
# possible_credentials() extracts new ones from the player's JavaScript.
CLIENT_ID = "2tiTbKxmQFwnbFjMONU4k7njMRZmV3ZMwRBndiZs"
CLIENT_SECRET = (
    "RDUJyAk4zFEGtQ8rsTmylDSfxmALRNBn3D1BsRr7MKi3oa1TL9Mq9QxqUPK7loiu"
    "mXolEWbJcWa4IGAhtwnTz1cSXClGJ1tkkNCNWwRwjxIKTZJKOJxbwaNt0Rm3WG0v"
)

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# How much Beatport's duration can differ from Discogs' to consider the track
# correct: 15 seconds or 8%, whichever is larger (printed durations are often
# rounded).
TOLERANCE_SECONDS = 15
TOLERANCE_PERCENTAGE = 0.08

# If the BPM we already had and Beatport's differ by less than this,
# we consider them "in agreement" (same criterion as analyze_bpm).
TOLERANCE_BPM = 2.5

# The anonymous token lasts 10 minutes; we renew it only when it expires.
_token = {"value": None, "expires": 0.0}


def credentials_from_embed():
    """Plan B: extract fresh client_id/client_secret from the embedded player's
    JavaScript (in case Beatport rotated the known ones)."""
    try:
        page = requests.get(BEATPORT_EMBED, headers=BROWSER, timeout=15).text
        bundle = re.search(r'src="(/static/main\.[0-9a-f]+\.js)"', page)
        if not bundle:
            return None
        js = requests.get(BEATPORT_EMBED.rstrip("/") + bundle.group(1), headers=BROWSER, timeout=20).text
    except requests.RequestException:
        return None
    client_id = re.search(r'client_id.{0,24}?"([A-Za-z0-9]{30,})"', js)
    client_secret = re.search(r'client_secret.{0,24}?"([A-Za-z0-9]{60,})"', js)
    if client_id and client_secret:
        return client_id.group(1), client_secret.group(1)
    return None


def possible_credentials():
    yield CLIENT_ID, CLIENT_SECRET
    fresh = credentials_from_embed()
    if fresh:
        yield fresh


def current_token():
    """Returns a valid anonymous token, or None if Beatport didn't provide one
    (no internet connection, or the embed scheme changed)."""
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    for client_id, client_secret in possible_credentials():
        try:
            resp = requests.post(
                BEATPORT_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=15,
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        data = resp.json()
        if data.get("access_token"):
            _token["value"] = data["access_token"]
            # Renew a minute before expiry, just in case.
            _token["expires"] = time.time() + data.get("expires_in", 600) - 60
            return _token["value"]
    return None


def search_beatport(artist, title, target_duration):
    """Searches for the track on Beatport and returns the API track dict that
    truly matches (artist, title, and duration), or None.

    Raises RuntimeError if the token is unavailable (to stop the run instead of
    printing "not found" a thousand times).
    """
    token = current_token()
    if token is None:
        raise RuntimeError("Beatport did not renew the anonymous token")

    # Beatport separates the remix name ("Juaan Remix") from the title;
    # for the search we use the bare title and check the remix later
    # against the candidates.
    query = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip() or title
    is_various = not artist or artist.lower() in ("various", "unknown")
    params = {"name": query, "per_page": 20}
    if not is_various:
        params["artist_name"] = artist

    try:
        resp = requests.get(
            f"{BEATPORT_API}/catalog/tracks/",
            params=params,
            headers={"Authorization": f"Bearer {token}", **BROWSER},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results") or []
    except (requests.RequestException, ValueError):
        return None

    best = []
    for track in results:
        name = track.get("name") or ""
        mix = (track.get("mix_name") or "").strip()
        # "Original Mix" adds nothing; any other mix is part of the
        # title ("Concrete Jungle (Juaan Remix)").
        full_title = name if mix.lower() in ("", "original mix", "original") else f"{name} ({mix})"
        if not looks_similar(full_title, title):
            continue

        if not is_various:
            names = [a.get("name", "") for a in track.get("artists") or []]
            if not any(looks_similar(n, artist, umbral=0.8) for n in names):
                continue

        track_duration = (track.get("length_ms") or 0) / 1000
        if target_duration and track_duration:
            tolerance = max(TOLERANCE_SECONDS, target_duration * TOLERANCE_PERCENTAGE)
            difference = abs(track_duration - target_duration)
            if difference > tolerance:
                continue
            best.append((difference, track))
        elif normalize(full_title) == normalize(title):
            # Without duration to compare, we only accept the exact title match
            # (otherwise, "Song" vs "Song (Remix)" picks any, and the remix
            # has different BPM and key).
            best.append((9999, track))

    if not best:
        return None
    return min(best, key=lambda pair: pair[0])[1]


def main():
    limit = None
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(__doc__)
            return

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    # Beatport is consulted for ALL tracks, whether they have BPM or not:
    # it's the reference source. We only skip tracks that already have a
    # Beatport response recorded in bpm_sources (even if it's "not found"),
    # so subsequent runs go straight to what's missing.
    cursor.execute(
        """
        SELECT tracks.id, tracks.title, tracks.duration_display, tracks.bpm,
               tracks.bpm_source, tracks.bpm_alt, tracks.bpm_needs_review,
               tracks.bpm_verified, tracks.key, tracks.isrc,
               COALESCE(tracks.artist, releases.artist) AS artist
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.key IS NULL
           OR NOT EXISTS (SELECT 1 FROM bpm_sources
                          WHERE track_id = tracks.id AND source = 'beatport')
        ORDER BY releases.artist, releases.title, tracks.id
        """
    )
    pending = cursor.fetchall()
    if limit:
        pending = pending[:limit]

    print(f"Tracks to check on Beatport: {len(pending)}\n")
    print("Connecting to Beatport (anonymous token from embedded player)...")
    if current_token() is None:
        print(
            "Could not get Beatport's anonymous token.\n"
            "It might be a connection issue, or Beatport changed its\n"
            "embedded player. Try again later; in the meantime\n"
            "the rest of the workflow still works (enrich_bpm.py, analyze_bpm.py)."
        )
        return
    print("Connected.\n")

    stats = {"bpm": 0, "keys": 0, "match": 0, "doubtful": 0, "isrc": 0}
    for i, row in enumerate(pending, start=1):
        # If the record has multiple artists we store them as
        # "Artist 1 / Artist 2"; for searching we use only the first one.
        artist = row["artist"].split(" / ")[0]
        label = f"[{i}/{len(pending)}] {row['artist']} - {row['title']}"

        try:
            candidate = search_beatport(
                artist, row["title"], parse_duration(row["duration_display"])
            )
        except RuntimeError as e:
            print(f"\nStopping here: {e}. What was saved so far is preserved.")
            break

        if not candidate:
            # We note that Beatport didn't have it (bpm is NULL), so the
            # next run won't ask again. If it appears on Beatport someday,
            # delete that row and run again.
            cursor.execute(
                "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm) VALUES (?, 'beatport', NULL)",
                (row["id"],),
            )
            conn.commit()
            print(f"{label} (not on Beatport)")
            time.sleep(0.6)
            continue

        updates = []

        mix = (candidate.get("mix_name") or "").strip()
        detail = candidate.get("name") or ""
        if mix:
            detail = f"{detail} ({mix})"

        card_bpm = candidate.get("bpm")
        if card_bpm:
            # The Beatport sheet sometimes has the tempo at half speed
            # (67 for a 134 BPM track): we adjust it to your collection's
            # range, keeping the original number noted.
            card_bpm = float(card_bpm)
            beatport_bpm = fit_to_range(card_bpm)
            if beatport_bpm != card_bpm:
                detail = f"{detail} (sheet says {card_bpm:g} BPM)"
            record_bpm_source(conn, row["id"], "beatport", beatport_bpm, detail)
            if row["bpm"] is None:
                # No previous BPM: Beatport's becomes the main one,
                # but NOT validated — you put the checkmark in the editor.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'beatport' WHERE id = ?",
                    (beatport_bpm, row["id"]),
                )
                stats["bpm"] += 1
                updates.append(f"{beatport_bpm:g} BPM")
            elif row["bpm_source"] == "manual":
                # You entered it: unchanged. Beatport's figure
                # remains visible as a source in the editor.
                updates.append(f"Beatport says {beatport_bpm:g} (keeping yours)")
            elif abs(row["bpm"] - beatport_bpm) <= TOLERANCE_BPM:
                # Beatport agrees: we adopt its figure (it's the official one)
                # and the doubt is resolved, but validation remains yours,
                # with one click in the editor.
                if row["bpm_verified"]:
                    updates.append("Beatport matches your validated value")
                else:
                    cursor.execute(
                        "UPDATE tracks SET bpm = ?, bpm_source = 'beatport',"
                        " bpm_alt = NULL, bpm_needs_review = 0 WHERE id = ?",
                        (beatport_bpm, row["id"]),
                    )
                    stats["match"] += 1
                    updates.append("Beatport matches (confirm in editor)")
            elif row["bpm_verified"]:
                # You had already validated it and Beatport says something else:
                # we don't overwrite your value, but reopen the doubt so you
                # look at it with both figures in sight.
                cursor.execute(
                    "UPDATE tracks SET bpm_alt = ?, bpm_needs_review = 1,"
                    " bpm_verified = 0 WHERE id = ?",
                    (beatport_bpm, row["id"]),
                )
                stats["doubtful"] += 1
                updates.append(
                    f"warning: was validated at {row['bpm']:g} but Beatport says {beatport_bpm:g}"
                )
            else:
                # They differ and the previous value was automatic: Beatport's
                # official figure wins, the other stays one click away.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'beatport',"
                    " bpm_alt = ?, bpm_needs_review = 1 WHERE id = ?",
                    (beatport_bpm, row["bpm"], row["id"]),
                )
                stats["doubtful"] += 1
                updates.append(
                    f"doubtful BPM (measured {row['bpm']:g}, Beatport says {beatport_bpm:g})"
                )
        else:
            # It's on Beatport but with no BPM set: we note it so we don't
            # ask again.
            cursor.execute(
                "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm, detail)"
                " VALUES (?, 'beatport', NULL, ?)",
                (row["id"], detail),
            )

        if row["key"] is None:
            key = normalize_key((candidate.get("key") or {}).get("name"))
            if key:
                cursor.execute(
                    "UPDATE tracks SET key = ?, key_source = 'beatport' WHERE id = ?",
                    (key, row["id"]),
                )
                stats["keys"] += 1
                updates.append(f"key {key} ({to_camelot(key)})")

        if not row["isrc"] and candidate.get("isrc"):
            cursor.execute(
                "UPDATE tracks SET isrc = ? WHERE id = ?",
                (candidate["isrc"], row["id"]),
            )
            stats["isrc"] += 1
            updates.append("ISRC")

        conn.commit()
        print(f"{label} -> {', '.join(updates) if updates else 'no updates'}")

        # Take it easy, the API is borrowed.
        time.sleep(0.6)

    conn.close()

    print("\n" + "=" * 50)
    print(
        f"Beatport: {stats['bpm']} new BPMs, {stats['keys']} keys, "
        f"{stats['match']} match the measured values, {stats['doubtful']} doubtful, "
        f"{stats['isrc']} ISRCs."
    )
    print("Remember: nothing validates itself — you put the checkmark in the editor.")
    print("Next step: python enrich_bandcamp.py  (covers/durations that are missing)")
    print("        or: python analyze_bpm.py      (measure what Beatport didn't have)")
    print("        or: python edit_bpm.py         (validate BPMs, source by source)")


if __name__ == "__main__":
    main()
