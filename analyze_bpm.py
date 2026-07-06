"""
analyze_bpm.py — STEP 5 (Beatport fallback)

For each track that still has no BPM (because Beatport didn't have it),
searches for it on Bandcamp, YouTube, or SoundCloud (in that order), downloads
the audio to a temporary folder, measures the tempo locally and saves the result,
noted as source "youtube" in bpm_sources (with the video it came from).
The audio is deleted right after analysis.

Bandcamp is tried first: for small electronic music labels it usually has
the original audio (not a repost), so when it's there, it's the most reliable
source. yt-dlp doesn't have a "search" mode for Bandcamp like it does for
YouTube/SoundCloud, so the bandcamp.com autocomplete API is used first to
find the track URL.

Each search engine is queried with each artist in the credit ("B.Love /
Jhobei" = two searches) and also with the vinyl's catalog number ("SEMID026 ..."):
small labels usually title their uploads by catalog number, not artist.

To avoid measuring any video, it compares the video duration with the one in
Discogs: if they don't match reasonably, it discards it. Tracks without
Discogs duration are accepted if the video lasts 2-15 minutes.

If no candidate passes the duration filter but one matches title and artist,
it measures it as a last resort: different duration almost always means
another edition of the same song (album version vs 12", or a misEntered
Discogs duration), and tempo doesn't change between editions. That rescue
result is ALWAYS marked as doubtful, with both durations noted in the source,
so you decide in the editor. Videos looking like "full EP / label preview"
are excluded from rescue: a mini-mix has multiple tempos and would measure
anything.

Tempo is measured with TWO different detectors (deeprhythm, a neural network
trained on electronic music, and librosa, the classic). If both agree, the
number is reliable — but NOTHING validates itself: you put the green checkmark
in the editor (python edit_bpm.py), seeing all sources. If they don't agree —
typical error of "one detector heard 89 where the other heard 134" — deeprhythm's
is saved anyway, but the track is marked as doubtful, with the other candidate
one click away in the editor.

Since tempo detectors sometimes return double or half, the result is adjusted
to the typical club music range (88–176). If your collection is different
(hip hop, ambient...), adjust BPM_MINIMO / BPM_MAXIMO in comunes.py.

How to run it:
    python analyze_bpm.py        # analyze all that are missing
    python analyze_bpm.py 5      # only 5 (for testing)

You can stop with Ctrl+C anytime: what's already analyzed is saved,
and next time it continues from where it left off.
"""

import difflib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
import librosa
import numpy as np
import requests
from yt_dlp import YoutubeDL

import config
from comunes import acomodar_al_rango, formatear_duracion, parsear_duracion
from db import get_connection, init_db, registrar_bpm_fuente

# If the two detectors differ by more than this (already adjusted to
# comunes.py range), the track is marked for manual review.
TOLERANCE_BPM = 2.5

# How much the video duration can differ from Discogs for it to be
# considered the correct track: 20 seconds or 12%, whichever is larger.
TOLERANCE_SECONDS = 20
TOLERANCE_PERCENTAGE = 0.12

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Public API (no key) used by bandcamp.com's search engine.
BANDCAMP_SEARCH_API = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

# Where to search for audio, in order, after trying Bandcamp.
# If YouTube enters anti-bot mode or doesn't have the track,
# SoundCloud is tried (where much of the small label music lives).
SEARCHERS = [
    ("YouTube", "ytsearch6"),
    ("SoundCloud", "scsearch6"),
]


def base_options():
    ops = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if config.YOUTUBE_COOKIES_NAVEGADOR:
        ops["cookiesfrombrowser"] = (config.YOUTUBE_COOKIES_NAVEGADOR,)
    return ops


def summarize_error(e):
    """Reduces yt-dlp error to one understandable line."""
    text = str(e).split("\n")[0]
    if "Sign in to confirm" in text:
        return "YouTube asking for login (anti-bot mode; usually clears in a few hours)"
    if "DRM protected" in text:
        return "served with DRM (cannot download)"
    return text[:120]


# Words that don't say anything about WHICH track it is (appear in any
# title) and so don't count for comparison.
EMPTY_WORDS = {"the", "and", "you", "your", "feat", "with", "mix", "original"}


def words(text):
    """Converts a title to a set of comparable words."""
    clean = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {p for p in clean.split() if len(p) > 2 and p not in EMPTY_WORDS}


def split_artists(artist):
    """Returns the list of artists from a composite Discogs credit
    ("B.Love / Jhobei" -> ["B.Love", "Jhobei"]). In a split or
    collaboration the track may be published under any of the names,
    so each must be searched and a video from either counts as good.
    For "Various" or "Unknown" returns empty list (no artist to check)."""
    if artist.lower() in ("various", "unknown"):
        return []
    return [part.strip() for part in artist.split(" / ") if part.strip()]


def artist_tokens(artist):
    """Comparable words from ALL artists in the credit, for checking
    'does the artist appear in the video?'."""
    tokens = set()
    for part in split_artists(artist):
        tokens |= words(part)
    return tokens


def build_queries(artist, title, catno):
    """The searches to try for a track: one for each artist in the credit
    and, if the vinyl has a catalog number, one more with it
    ("SEMID026 R U Listening..."): small labels usually title their uploads
    by catalog number, not artist."""
    queries = [f"{part} {title}" for part in split_artists(artist)] or [title]
    if catno:
        queries.append(f"{catno} {title}")
    return queries


# Signs that the video is not ONE track but the whole EP or a label
# preview ("R U Listening EP inc Sweely Remix", 8:34).
# They pass the title and artist checks and only duration reveals them;
# that's why rescue (which loosens duration) excludes them: a mini-mix
# blends multiple tempos and would measure anything.
COMPILATION_WORDS = {"ep", "lp", "va", "inc", "incl", "minimix", "megamix",
                     "preview", "previews", "snippet", "snippets",
                     "sampler", "showreel", "teaser"}


def seems_compilation(video_title):
    clean = "".join(c.lower() if c.isalnum() else " " for c in video_title)
    return bool(set(clean.split()) & COMPILATION_WORDS)


# Threshold for character-by-character comparison (see partial_similarity):
# above this we treat it as "same title".
FUZZY_THRESHOLD = 0.75


def compact(text):
    """Leaves only lowercase letters and numbers, no spaces or punctuation —
    to compare "Snap-Shot" with "Snapshot" or "Sugar Coated" with "Sugarcoated"
    as the same text."""
    return "".join(c.lower() for c in text if c.isalnum())


def partial_similarity(a, b):
    """How well the shorter of the two texts fits INSIDE the longer one,
    character by character. Unlike comparing the two strings end-to-end,
    this doesn't penalize the candidate bringing extra decoration
    (label name, artist, etc.)."""
    a, b = compact(a), compact(b)
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    best = 0.0
    for i in range(len(long) - len(short) + 1):
        best = max(best, difflib.SequenceMatcher(None, short, long[i:i + len(short)]).ratio())
        if best == 1.0:
            break
    return best


def title_matches(track_title, candidate_title):
    """True if the candidate seems to be the same song: shares half the words
    with the Discogs one (normal check), or — when written differently, hyphenated,
    without space, or with one extra/missing letter, as often happens on Bandcamp —
    the text is similar enough character by character."""
    target = words(track_title)
    candidate_words = words(candidate_title)
    if target and len(target & candidate_words) / len(target) >= 0.5:
        return True
    return partial_similarity(track_title, candidate_title) >= FUZZY_THRESHOLD


def select_videos(candidates, artist, track_title, target_duration):
    """Separates search results into two lists and returns (approved, rescue),
    both sorted from best to worst duration match. Approved ones pass all three
    checks; rescue ones match title and artist but NOT duration, serving as
    a last resort (see rescue in analyze_track). They're lists not single videos
    so if the best one fails to download — SoundCloud serves some tracks with
    DRM — the next one can be tried.

    Three checks, because each one alone can fail:
      - the video title must be (or closely match) the track title (otherwise,
        in an EP the search gives you another song by the same artist with
        similar duration),
      - the artist must appear in the video title or the channel that uploaded it
        (otherwise, "Free The Drums" matches any video saying "FREE DOWNLOAD ... drums"),
        and
      - the duration must match Discogs' (if available); if not, it picks any of
        "Song X" vs "Song X (Remix)".
    """
    tokens = artist_tokens(artist)

    approved = []
    rescue = []
    for video in candidates:
        dur = video.get("duration")
        if not dur:
            continue

        video_title = video.get("title", "")
        if not title_matches(track_title, video_title):
            continue

        channel = video.get("uploader") or video.get("channel") or ""
        if tokens and not tokens & (words(video_title) | words(channel)):
            continue

        if target_duration:
            tolerance = max(TOLERANCE_SECONDS, target_duration * TOLERANCE_PERCENTAGE)
            difference = abs(dur - target_duration)
            if difference <= tolerance:
                approved.append((difference, video))
            elif 120 <= dur <= 900 and not seems_compilation(video_title):
                rescue.append((difference, video))
        elif 120 <= dur <= 900:
            approved.append((0, video))
    approved.sort(key=lambda pair: pair[0])
    rescue.sort(key=lambda pair: pair[0])
    return [v for _, v in approved], [v for _, v in rescue]


def search_bandcamp(artist, title, target_duration, catno=None):
    """Searches for the track on Bandcamp and returns (approved, rescue)
    like select_videos, with dicts {title, url, duration, uploader}.

    Bandcamp doesn't have a "search" mode in yt-dlp, so we first query
    bandcamp.com's autocomplete API (the same the site's search uses)
    for candidates, filter by title/artist like in select_videos, and only
    for those that match by text do we ask yt-dlp for real duration
    (bandcamp.com search doesn't include it) to confirm it's the right track
    before downloading audio.
    """
    queries = build_queries(artist, title, catno)
    tokens = artist_tokens(artist)

    candidates = []
    seen = set()
    for query in queries:
        try:
            resp = requests.post(
                BANDCAMP_SEARCH_API,
                json={
                    "search_text": query,
                    "search_filter": "track",
                    "full_page": False,
                    "fan_id": None,
                },
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("auto", {}).get("results") or []
        except (requests.RequestException, ValueError):
            continue

        for r in results:
            if r.get("type") != "t" or not r.get("item_url_path"):
                continue
            if r["item_url_path"] in seen:
                continue
            seen.add(r["item_url_path"])
            if not title_matches(title, r.get("name", "")):
                continue
            if tokens and not tokens & words(r.get("band_name", "")):
                continue
            candidates.append(r)

    rescue = []
    for candidate in candidates[:3]:
        url = candidate["item_url_path"]
        try:
            with YoutubeDL(base_options()) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            continue

        dur = info.get("duration")
        if not dur:
            continue
        video = {
            "title": info.get("title") or f"{candidate.get('band_name', '')} - {candidate.get('name', '')}",
            "url": url,
            "duration": dur,
            "uploader": candidate.get("band_name", ""),
        }
        if target_duration:
            tolerance = max(TOLERANCE_SECONDS, target_duration * TOLERANCE_PERCENTAGE)
            if abs(dur - target_duration) <= tolerance:
                return [video], rescue  # one that passes everything is enough
            if 120 <= dur <= 900 and not seems_compilation(video["title"]):
                rescue.append(video)
        elif 120 <= dur <= 900:
            return [video], rescue
    return [], rescue


# The deeprhythm model takes a few seconds to load (and the first time
# downloads its weights from the internet), so we load it just once,
# only when needed.
_deeprhythm_model = None


def deeprhythm_model():
    global _deeprhythm_model
    if _deeprhythm_model is None:
        from deeprhythm import DeepRhythmPredictor
        _deeprhythm_model = DeepRhythmPredictor()
    return _deeprhythm_model


def measure_bpm(audio_path, video_duration):
    """Cuts a piece from the middle of the track (where the beat has come in),
    converts it to WAV, and measures tempo with both detectors.

    Returns (bpm, alternative, doubtful):
      - detectors agree:      (bpm, None, False) — reliable number
        (you still validate it in edit_bpm.py, nothing validates itself),
      - detectors disagree:   (deeprhythm, librosa, True) —
        the first is saved, marked for confirmation in edit_bpm.py,
      - only one measured:    (that bpm, None, True if librosa),
      - couldn't measure any: (None, None, False).

    Why two detectors? Because librosa sometimes locks onto a beat it shouldn't
    (measures 89 on a 134 BPM track: a 2/3 error no range adjustment can fix).
    deeprhythm is much more precise on electronic music, and agreement between
    both tells us if the number is trustworthy.
    """
    wav = audio_path.with_suffix(".wav")
    start = min(60, int(video_duration // 3)) if video_duration else 30
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-ss", str(start), "-i", str(audio_path),
         "-t", "60", "-ac", "1", "-ar", "22050", str(wav)],
        check=True,
    )

    # Detector 1: deeprhythm (note: needs WAV, doesn't read webm/m4a).
    try:
        bpm_dr = acomodar_al_rango(float(deeprhythm_model().predict(str(wav))))
    except Exception:
        bpm_dr = None

    # Detector 2: librosa.
    y, sr = librosa.load(str(wav), sr=None, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm_lr = acomodar_al_rango(float(np.atleast_1d(tempo)[0]))

    if bpm_dr is None and bpm_lr is None:
        return None, None, False
    if bpm_lr is None:
        return bpm_dr, None, False   # deeprhythm alone: reliable
    if bpm_dr is None:
        return bpm_lr, None, True    # librosa alone: better to confirm
    if abs(bpm_dr - bpm_lr) <= TOLERANCE_BPM:
        return bpm_dr, None, False   # two detectors agree
    return bpm_dr, bpm_lr, True


def download_and_measure(video, tmpdir):
    """Downloads the video's audio to tmpdir, measures tempo, and deletes the
    file. Returns the same as measure_bpm. If download fails (e.g., SoundCloud
    serving the track with DRM), lets the exception bubble up so the caller
    can try another candidate."""
    download_opts = base_options()
    download_opts.update(
        {
            "noprogress": True,
            "format": "bestaudio/best",
            "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        }
    )
    try:
        with YoutubeDL(download_opts) as ydl:
            info = ydl.extract_info(video["url"], download=True)
            audio_path = Path(ydl.prepare_filename(info))
        return measure_bpm(audio_path, video.get("duration"))
    finally:
        # delete the audio as soon as we measure it (or what's left
        # of a failed download)
        for file in Path(tmpdir).iterdir():
            file.unlink()


def analyze_track(artist, title, target_duration, tmpdir, catno=None):
    """Searches for the track (Bandcamp first, YouTube and SoundCloud if not),
    downloads the best candidate, and returns (bpm, alternative, doubtful, detail).
    If no candidate passes the duration filter but one matches title and artist,
    it measures it anyway as a last resort (see rescue pass below).
    If nothing works, bpm is None and detail explains why."""
    queries = build_queries(artist, title, catno)

    reasons = []
    rescues = []  # (searcher, video) matching everything except duration
    for name, prefix in [("Bandcamp", None)] + SEARCHERS:
        try:
            if prefix is None:
                videos, rescue = search_bandcamp(artist, title, target_duration, catno)
            else:
                search_opts = base_options()
                search_opts["extract_flat"] = "in_playlist"
                candidates = []
                seen = set()
                with YoutubeDL(search_opts) as ydl:
                    for query in queries:
                        search = ydl.extract_info(f"{prefix}:{query}", download=False)
                        for entry in search.get("entries") or []:
                            key = entry.get("url") or entry.get("id")
                            if key in seen:
                                continue
                            seen.add(key)
                            candidates.append(entry)
                videos, rescue = select_videos(candidates, artist, title, target_duration)
        except Exception as e:
            reasons.append(f"{name}: {summarize_error(e)}")
            continue

        rescues.extend((name, video) for video in rescue)
        if not videos:
            reasons.append(f"{name}: no result matching artist, title, and duration")
            continue

        # If the best candidate fails to download (typical: SoundCloud
        # serves it with DRM), try the next ones: often there's another
        # upload of the same song that does download.
        for video in videos[:3]:
            try:
                bpm, alternative, doubtful = download_and_measure(video, tmpdir)
            except Exception as e:
                reasons.append(f"{name}: {summarize_error(e)}")
                continue
            if bpm is None:
                reasons.append(f"{name}: couldn't measure clear tempo")
                continue
            return bpm, alternative, doubtful, f"{video.get('title', '')} [{name}]"

    # Rescue pass: no one passed the complete filter, but these candidates
    # match title and artist and only fail on duration. It's almost always
    # another edition of the same song (album version vs 12", or misEntered
    # Discogs duration), and tempo doesn't change between editions. But the
    # result is ALWAYS doubtful, with both durations noted, so you have
    # the final say in the editor.
    rescues.sort(key=lambda pair: abs(pair[1]["duration"] - target_duration))
    for name, video in rescues[:2]:
        try:
            bpm, alternative, _ = download_and_measure(video, tmpdir)
        except Exception as e:
            reasons.append(f"{name}: {summarize_error(e)}")
            continue
        if bpm is None:
            reasons.append(f"{name}: couldn't measure clear tempo")
            continue
        detail = (f"{video.get('title', '')} [{name}; note: lasts "
                  f"{formatear_duracion(video['duration'])} but Discogs says "
                  f"{formatear_duracion(target_duration)} — different edition?]")
        return bpm, alternative, True, detail

    return None, None, False, " | ".join(reasons)


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
    cursor.execute(
        """
        SELECT tracks.id, tracks.title, tracks.duration_display,
               COALESCE(tracks.artist, releases.artist) AS artist,
               releases.catno
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm IS NULL
        ORDER BY releases.artist, releases.title, tracks.id
        """
    )
    pending = cursor.fetchall()
    if limit:
        pending = pending[:limit]

    print(f"Tracks to analyze: {len(pending)}")
    print("(this downloads audio from YouTube and measures it here; takes ~30s per track,")
    print(" you can stop with Ctrl+C and resume later)\n")

    found = 0
    doubtful = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pending, start=1):
            label = f"[{i}/{len(pending)}] {row['artist']} - {row['title']}"
            try:
                bpm, alternative, doubt, detail = analyze_track(
                    row["artist"], row["title"],
                    parsear_duracion(row["duration_display"]), tmpdir,
                    row["catno"],
                )
            except KeyboardInterrupt:
                print("\nStopped. What was analyzed is saved.")
                break
            except Exception as e:
                print(f"{label}\n   -> error, continuing: {e}")
                continue

            if bpm:
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'youtube',"
                    " bpm_alt = ?, bpm_needs_review = ?, bpm_verified = 0 WHERE id = ?",
                    (bpm, alternative, int(doubt), row["id"]),
                )
                registrar_bpm_fuente(conn, row["id"], "youtube", bpm, detail)
                conn.commit()
                found += 1
                doubtful += int(doubt)
                if doubt and alternative:
                    warning = f"  [DOUBTFUL: other detector measured {alternative:g}]"
                elif doubt:
                    warning = "  [DOUBTFUL: only one detector measured]"
                else:
                    warning = ""
                print(f"{label} -> {bpm:g} BPM{warning}\n   (measured from: {detail})")
            else:
                print(f"{label}\n   -> {detail}")

            # Pause between tracks to not trigger YouTube's anti-bot.
            time.sleep(3)

    conn.close()
    print("\n" + "=" * 50)
    print(f"BPM measured for {found} of {len(pending)} tracks.")
    if doubtful:
        print(f"{doubtful} were marked as doubtful (detectors didn't")
        print("agree), with the other candidate one click away in the editor.")
    else:
        print("Both detectors agreed on all: good sign.")
    print("Your turn: validate them in the editor: python edit_bpm.py")


if __name__ == "__main__":
    main()
