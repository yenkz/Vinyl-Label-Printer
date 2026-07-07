"""
download_music.py — Download a digital copy of your collection from Soulseek.

Reuses the same database the labels are built from (vinyl_labels.db): for each
record you own on vinyl, it looks the music up on Soulseek and downloads it,
preferring lossless. The result is a tidy, DJ-ready library on disk:

    ~/Music/Vinyl/<Artist> - <Album> (<CATNO>)/<position> <Title>.<ext>

with tags and the record cover embedded in every file.

How it finds the music
----------------------
Soulseek is a peer-to-peer network: other people share folders of files. The
heavy lifting (logging in, searching, queueing, retrying) is done by a small
background program called *slskd*, which you run once and leave open; this
script just tells it what to look for and where to put the results. See the
README section "Download digital copies (Soulseek)" to set it up.

For each record it first searches for the WHOLE album from a single person
(one folder = consistent quality and source), matches that folder's files to
your track list by title, and downloads the best-format copy of each. Anything
that folder didn't have is then searched for track by track.

Format preference (best first): AIFF > FLAC > WAV > MP3 320. Whatever is found
is kept as-is — no conversion (rekordbox/Serato read all four).

How to run it
-------------
    python download_music.py                # everything still missing
    python download_music.py aphex          # only records containing "aphex"
    python download_music.py --force        # re-download even if already present

You can stop with Ctrl+C anytime: each finished track is saved immediately, so
next time it continues from where it left off. Records already fully downloaded
are skipped without touching the network.
"""

import re
import shutil
import sys
import time
from pathlib import Path

import slskd_api

import config
from common import parse_duration, looks_similar
from db import get_connection, init_db

# Audio we accept, and how strongly we prefer each one (higher = better),
# following your order AIFF > FLAC > WAV > MP3 320 > lower-bitrate MP3.
def format_rank(f):
    """Preference score for a Soulseek file; -1 means 'not audio we want'."""
    ext = (f.get("extension") or "").lower().lstrip(".")
    if not ext:  # slskd sometimes leaves 'extension' empty; read it off the name
        base = remote_basename(f.get("filename", ""))
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext in ("aiff", "aif"):
        return 5
    if ext == "flac":
        return 4
    if ext == "wav":
        return 3
    if ext == "mp3":
        return 2 if (f.get("bitRate") or 0) >= 320 else 1
    return -1


# Soulseek paths use Windows-style backslashes ("@@abc\Music\Album\01 Track.flac"),
# so we normalize before splitting off the folder and the file name.
def remote_basename(remote_path):
    return remote_path.replace("\\", "/").rsplit("/", 1)[-1]


def remote_dir(remote_path):
    norm = remote_path.replace("\\", "/")
    return norm.rsplit("/", 1)[0] if "/" in norm else ""


# A leading track number or vinyl position on a shared file ("A1 ", "01 - ",
# "1.") says nothing about WHICH song it is, so we drop it before comparing
# the file name to your track title.
LEADING_INDEX = re.compile(r"^\s*([A-F]?\d{1,2}|[A-F])[\s._)\-]+", re.IGNORECASE)


def track_stem(remote_path):
    """File name with no folder, extension, or leading index — ready to compare."""
    base = remote_basename(remote_path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return LEADING_INDEX.sub("", stem).strip()


def safe_name(text):
    """A version of a name safe to use as a file or folder on disk."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text or "")
    return text.strip().strip(".") or "Unknown"


# =========================================================
# Talking to slskd (search + download)
# =========================================================
def run_search(client, query, timeout_ms=8000):
    """Runs one Soulseek search and returns its responses (one per person).
    Waits until slskd marks the search finished, then cleans it up."""
    state = client.searches.search_text(
        searchText=query,
        filterResponses=True,
        searchTimeout=timeout_ms,
    )
    search_id = state["id"]
    deadline = time.time() + timeout_ms / 1000 + 10
    while time.time() < deadline:
        if client.searches.state(search_id).get("isComplete"):
            break
        time.sleep(1)
    try:
        return client.searches.search_responses(search_id)
    finally:
        try:
            client.searches.delete(search_id)
        except Exception:
            pass


def enqueue_and_wait(client, username, files, timeout_s=300):
    """Asks slskd to download `files` from `username` and waits for them to
    finish. Returns the set of remote file names that completed successfully."""
    wanted = {f["filename"] for f in files}
    try:
        if not client.transfers.enqueue(username, files):
            return set()
    except Exception:
        return set()

    done, failed = set(), set()
    deadline = time.time() + timeout_s
    while time.time() < deadline and len(done) + len(failed) < len(wanted):
        time.sleep(3)
        try:
            transfer = client.transfers.get_downloads(username)
        except Exception:
            continue
        for directory in transfer.get("directories", []):
            for tf in directory.get("files", []):
                name = tf.get("filename")
                if name not in wanted:
                    continue
                state = tf.get("state", "")
                if "Succeeded" in state:
                    done.add(name)
                elif "Completed" in state:  # Errored / Cancelled / TimedOut / Rejected
                    failed.add(name)
    return done


# =========================================================
# Matching Soulseek results to your tracks
# =========================================================
def match_folder(files, tracks):
    """Best file in one shared folder for each of your tracks: {track_id: file}."""
    matched = {}
    for track in tracks:
        best = None
        for f in files:
            if f.get("isLocked") or format_rank(f) < 0:
                continue
            if looks_similar(track_stem(f["filename"]), track["title"]):
                if best is None or format_rank(f) > format_rank(best):
                    best = f
        if best is not None:
            matched[track["id"]] = best
    return matched


def album_candidates(responses, tracks):
    """All (username, {track_id: file}) folders that cover at least one track,
    best first. 'Best' = covers the most tracks, in the best format, from the
    fastest freely-available uploader."""
    candidates = []
    for r in responses:
        by_folder = {}
        for f in r.get("files", []):
            by_folder.setdefault(remote_dir(f["filename"]), []).append(f)
        for files in by_folder.values():
            matched = match_folder(files, tracks)
            if not matched:
                continue
            avg_rank = sum(format_rank(f) for f in matched.values()) / len(matched)
            score = (
                len(matched),
                avg_rank,
                int(r.get("hasFreeUploadSlot", False)),
                r.get("uploadSpeed", 0),
                -r.get("queueLength", 0),
            )
            candidates.append((score, r["username"], matched))
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates


def best_track_file(client, release, track):
    """Per-track fallback: search '<artist> <title>' and return (file, username)
    for the best single match, preferring format then closeness to the Discogs
    duration. (file, None) is never returned — it's (None, None) when nothing fits."""
    artist = first_artist(track["artist"] or release["artist"])
    target = parse_duration(track["duration_display"])
    queries = [q for q in (f"{artist} {track['title']}".strip(), track["title"]) if q]

    best_file, best_user, best_key = None, None, None
    for query in queries:
        for r in run_search(client, query):
            for f in r.get("files", []):
                if f.get("isLocked") or format_rank(f) < 0:
                    continue
                if not looks_similar(track_stem(f["filename"]), track["title"]):
                    continue
                duration_penalty = abs((f.get("length") or 0) - target) if target else 0
                key = (
                    format_rank(f),
                    int(r.get("hasFreeUploadSlot", False)),
                    -duration_penalty,
                    r.get("uploadSpeed", 0),
                    -r.get("queueLength", 0),
                )
                if best_key is None or key > best_key:
                    best_file, best_user, best_key = f, r["username"], key
        if best_file is not None:
            break  # good enough from the more specific query; don't widen
    return best_file, best_user


# =========================================================
# Putting files in place, with tags and cover
# =========================================================
def cover_bytes(release):
    """The record cover downloaded by the label steps, as bytes (or None)."""
    if not release["cover_path"]:
        return None
    path = Path(__file__).parent / release["cover_path"]
    return path.read_bytes() if path.exists() else None


def tag_file(path, track, release):
    """Writes title/artist/album/track/year/label and embeds the cover.
    FLAC uses Vorbis comments; MP3/AIFF/WAV use ID3."""
    ext = path.suffix.lower().lstrip(".")
    cover = cover_bytes(release)
    artist = track["artist"] or release["artist"] or ""

    if ext == "flac":
        from mutagen.flac import FLAC, Picture

        audio = FLAC(str(path))
        audio["title"] = track["title"] or ""
        audio["artist"] = artist
        audio["album"] = release["title"] or ""
        audio["albumartist"] = release["artist"] or ""
        if track["position"]:
            audio["tracknumber"] = str(track["position"])
        if release["year"]:
            audio["date"] = str(release["year"])
        if release["label"]:
            audio["organization"] = release["label"]
        if release["catno"]:
            audio["catalognumber"] = release["catno"]
        if cover:
            picture = Picture()
            picture.type = 3  # front cover
            picture.mime = "image/jpeg"
            picture.data = cover
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()
        return

    # MP3 / AIFF / WAV — all tagged with ID3, via the right container class.
    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPUB, TRCK

    if ext in ("aiff", "aif"):
        from mutagen.aiff import AIFF as Container
    elif ext == "wav":
        from mutagen.wave import WAVE as Container
    else:
        from mutagen.mp3 import MP3 as Container

    audio = Container(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    tags.setall("TIT2", [TIT2(encoding=3, text=track["title"] or "")])
    tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    tags.setall("TALB", [TALB(encoding=3, text=release["title"] or "")])
    tags.setall("TPE2", [TPE2(encoding=3, text=release["artist"] or "")])
    if track["position"]:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track["position"]))])
    if release["year"]:
        tags.setall("TDRC", [TDRC(encoding=3, text=str(release["year"]))])
    if release["label"]:
        tags.setall("TPUB", [TPUB(encoding=3, text=release["label"])])
    if cover:
        tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover)])
    audio.save()


def locate_download(f, track):
    """Finds the finished file on disk in slskd's downloads folder. Tries the
    exact name first; if slskd renamed it, falls back to the same extension plus
    a fuzzy title match."""
    root = Path(config.SLSKD_DOWNLOADS_DIR).expanduser()
    if not root.exists():
        return None
    base = remote_basename(f["filename"])
    exact = [p for p in root.rglob(base) if p.is_file()]
    if exact:
        return max(exact, key=lambda p: p.stat().st_mtime)
    ext = (f.get("extension") or (base.rsplit(".", 1)[-1] if "." in base else "")).lower().lstrip(".")
    if not ext:
        return None
    fuzzy = [
        p for p in root.rglob(f"*.{ext}")
        if p.is_file() and looks_similar(LEADING_INDEX.sub("", p.stem).strip(), track["title"])
    ]
    return max(fuzzy, key=lambda p: p.stat().st_mtime) if fuzzy else None


def place_and_tag(src, track, release):
    """Moves the downloaded file into the library, names it by vinyl position,
    tags it, and returns its final path."""
    ext = src.suffix.lower()
    folder = f"{release['artist']} - {release['title']}"
    if release["catno"]:
        folder += f" ({release['catno']})"
    dest_dir = Path(config.MUSIC_DIR).expanduser() / safe_name(folder)
    dest_dir.mkdir(parents=True, exist_ok=True)

    prefix = f"{track['position']} " if track["position"] else ""
    dest = dest_dir / f"{safe_name(prefix + (track['title'] or 'Untitled'))}{ext}"
    shutil.move(str(src), str(dest))
    try:
        tag_file(dest, track, release)
    except Exception as e:  # a tagging hiccup shouldn't lose the download
        print(f"      (couldn't write tags: {e})")
    return dest


# =========================================================
# Orchestration
# =========================================================
def first_artist(artist):
    """Leading artist of a composite Discogs credit ("B.Love / Jhobei" ->
    "B.Love"); "" for Various/compilations, where the artist isn't a useful query."""
    artist = (artist or "").strip()
    if artist.lower().startswith(("various", "unknown")):
        return ""
    return artist.split(" / ")[0].strip()


def album_queries(release):
    """Searches to try for a whole record, most specific first."""
    artist = first_artist(release["artist"])
    queries = []
    if artist:
        queries.append(f"{artist} {release['title']}")
    queries.append(release["title"] or "")
    if release["catno"]:
        queries.append(f"{release['catno']} {release['title']}")
    seen, unique = set(), []
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            unique.append(q)
    return unique


def has_audio(track):
    """True if this track already has a downloaded file that still exists."""
    path = track["audio_path"]
    return bool(path) and Path(path).exists()


def save_track_audio(conn, track_id, dest, username):
    conn.execute(
        "UPDATE tracks SET audio_path = ?, audio_format = ?, audio_source = ? WHERE id = ?",
        (str(dest), dest.suffix.lower().lstrip("."), username, track_id),
    )
    conn.commit()


def process_release(client, conn, release, tracks):
    """Downloads everything still missing for one record. Returns
    (num_downloaded, list_of_missing_tracks)."""
    remaining = [t for t in tracks if not has_audio(t)]
    if not remaining:
        return 0, []
    by_id = {t["id"]: t for t in remaining}
    got = {}

    # 1) Whole-album pass: find a folder covering as many tracks as possible.
    candidates = []
    for query in album_queries(release):
        candidates = album_candidates(run_search(client, query), remaining)
        if candidates and candidates[0][0][0] == len(remaining):
            break  # a single folder has the entire record; stop searching
        time.sleep(1)

    for _, username, matched in candidates[:5]:
        pending = {tid: f for tid, f in matched.items() if tid not in got}
        if not pending:
            continue
        done = enqueue_and_wait(client, username, list(pending.values()))
        for tid, f in pending.items():
            if f["filename"] not in done:
                continue
            local = locate_download(f, by_id[tid])
            if local:
                dest = place_and_tag(local, by_id[tid], release)
                save_track_audio(conn, tid, dest, username)
                got[tid] = dest
        if len(got) == len(remaining):
            break

    # 2) Per-track pass for whatever the album folder didn't provide.
    for track in remaining:
        if track["id"] in got:
            continue
        f, username = best_track_file(client, release, track)
        if not f:
            continue
        if f["filename"] in enqueue_and_wait(client, username, [f]):
            local = locate_download(f, track)
            if local:
                dest = place_and_tag(local, track, release)
                save_track_audio(conn, track["id"], dest, username)
                got[track["id"]] = dest

    missing = [t for t in remaining if t["id"] not in got]
    return len(got), missing


def connect():
    """Connects to slskd and returns the client, or None with an explanation."""
    if not config.SLSKD_API_KEY:
        print("SLSKD_API_KEY is empty. Put slskd's API key in your .env file.")
        print('See the README section "Download digital copies (Soulseek)".')
        return None
    try:
        client = slskd_api.SlskdClient(
            host=config.SLSKD_HOST,
            api_key=config.SLSKD_API_KEY,
            url_base=config.SLSKD_URL_BASE,
        )
        state = client.application.state()
    except Exception:
        print(f"Couldn't reach slskd at {config.SLSKD_HOST}.")
        print("Start it in another terminal with:  make slskd")
        return None
    server = (state.get("server") or {}).get("state", "")
    if "Connected" not in server:
        print("slskd is running but not logged in to Soulseek yet.")
        print("Check the Soulseek username/password in your slskd.yml, then retry.")
        return None
    return client


def main():
    arguments = sys.argv[1:]
    force = "--force" in arguments
    filter_text = next((a.lower() for a in arguments if not a.startswith("--")), "")

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()
    if not releases:
        print("Your collection is empty. Run first: python fetch_discogs.py")
        return

    if filter_text:
        releases = [r for r in releases if filter_text in f"{r['artist']} {r['title']}".lower()]
        if not releases:
            print(f"No record in your collection contains '{filter_text}'.")
            return

    client = connect()
    if client is None:
        return

    print(f"Records to process: {len(releases)}")
    print("(searching Soulseek and downloading; you can stop with Ctrl+C and resume later)\n")

    total_downloaded = 0
    complete = 0
    not_found = []
    try:
        for i, release in enumerate(releases, start=1):
            cursor.execute(
                "SELECT * FROM tracks WHERE release_id = ? ORDER BY id",
                (release["release_id"],),
            )
            tracks = cursor.fetchall()
            if not tracks:
                continue

            label = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
            if not force and all(has_audio(t) for t in tracks):
                print(f"{label}: already downloaded, skipping")
                complete += 1
                continue

            if force:
                # Re-download: forget existing files so they count as missing.
                cursor.execute(
                    "UPDATE tracks SET audio_path = NULL WHERE release_id = ?",
                    (release["release_id"],),
                )
                conn.commit()
                tracks = cursor.execute(
                    "SELECT * FROM tracks WHERE release_id = ? ORDER BY id",
                    (release["release_id"],),
                ).fetchall()

            print(f"{label}: searching...")
            downloaded, missing = process_release(client, conn, release, tracks)
            total_downloaded += downloaded
            if not missing:
                complete += 1
            for t in missing:
                not_found.append(f"{release['artist']} - {t['title']}")
            summary = f"{downloaded} downloaded"
            if missing:
                summary += f", {len(missing)} not found"
            print(f"   -> {summary}")
    except KeyboardInterrupt:
        print("\nStopped. What was downloaded is saved; run again to continue.")

    conn.close()
    print("\n" + "=" * 50)
    print(f"Downloaded {total_downloaded} tracks. Records complete: {complete}/{len(releases)}.")
    print(f"Your library: {Path(config.MUSIC_DIR).expanduser()}")
    if not_found:
        print(f"\n{len(not_found)} tracks weren't found on Soulseek "
              "(try them by hand there, or buy them on Bandcamp):")
        for line in not_found:
            print(f"  - {line}")


if __name__ == "__main__":
    main()
