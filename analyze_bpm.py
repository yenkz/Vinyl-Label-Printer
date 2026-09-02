"""
analyze_bpm.py — STEP 5 (Beatport fallback)

For each track that still has no BPM or musical key (because Beatport didn't
have it),
searches for it on Bandcamp, YouTube, or SoundCloud (in that order), downloads
the audio to a temporary folder, measures tempo and tonality locally, and saves
the results with their detector provenance.
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

Key is also measured with TWO detectors. Essentia's EDM-oriented ``bgate``
profile is the primary estimate; librosa independently compares the track's
harmonic CQT chroma against major/minor key profiles. Exact agreement is saved
as confirmed. A disagreement or single-detector result is saved for review with
the alternative visible in the editor. Key uses the complete downloaded track;
the 60-second middle excerpt remains specific to BPM.

Since tempo detectors sometimes return double or half, the result is adjusted
to the typical club music range (88–176). If your collection is different
(hip hop, ambient...), adjust BPM_MIN / BPM_MAX in common.py.

How to run it:
    python analyze_bpm.py        # missing BPMs/keys on newly imported records
    python analyze_bpm.py 5      # only 5 (for testing)
    python analyze_bpm.py --all  # retry old missing BPMs/keys too
    python analyze_bpm.py 20 --pace 8  # wait 8s between tracks in this batch

You can stop with Ctrl+C anytime: what's already analyzed is saved,
and next time it continues from where it left off.
"""

import argparse
import difflib
import math
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import imageio_ffmpeg
import librosa
import numpy as np
import requests
from yt_dlp import YoutubeDL

import config
from common import fit_to_range, format_duration, normalize_key, parse_duration
from db import (
    get_connection,
    init_db,
    mark_workflow_step,
    record_bpm_source,
    record_key_source,
)

# If the two detectors differ by more than this (already adjusted to
# common.py range), the track is marked for manual review.
TOLERANCE_BPM = 2.5

# How much the video duration can differ from Discogs for it to be
# considered the correct track: 20 seconds or 12%, whichever is larger.
TOLERANCE_SECONDS = 20
TOLERANCE_PERCENTAGE = 0.12

# Delay between tracks. It can be overridden for one run with --pace.
DEFAULT_PACE_SECONDS = 3.0

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


@dataclass
class KeySourceEstimate:
    source: str
    key: str
    strength: float | None = None


@dataclass
class AudioAnalysis:
    bpm: float | None = None
    bpm_alt: float | None = None
    bpm_doubtful: bool = False
    key: str | None = None
    key_alt: str | None = None
    key_doubtful: bool = False
    key_strength: float | None = None
    key_estimates: list[KeySourceEstimate] = field(default_factory=list)


def base_options():
    ops = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if config.YOUTUBE_COOKIES_BROWSER:
        ops["cookiesfrombrowser"] = (config.YOUTUBE_COOKIES_BROWSER,)
    return ops


def summarize_error(e):
    """Reduces yt-dlp error to one understandable line."""
    text = str(e).split("\n")[0]
    if "Sign in to confirm" in text:
        return "YouTube asking for login (anti-bot mode; usually clears in a few hours)"
    if "DRM protected" in text:
        return "served with DRM (cannot download)"
    return text[:120]


def non_negative_seconds(value):
    """Argparse type for a finite delay of zero seconds or more."""
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("pace must be a number of seconds") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("pace must be zero or more seconds")
    return seconds


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        description="Measure missing BPMs and musical keys from downloaded audio."
    )
    parser.add_argument("limit", nargs="?", type=int, help="maximum tracks to analyze")
    parser.add_argument(
        "--all",
        action="store_true",
        help="retry old tracks that are still missing BPM or key",
    )
    parser.add_argument(
        "--pace",
        type=non_negative_seconds,
        default=DEFAULT_PACE_SECONDS,
        metavar="SECONDS",
        help=("wait this many seconds between tracks "
              f"(default: {DEFAULT_PACE_SECONDS:g})"),
    )
    args = parser.parse_args(arguments)
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be a positive integer")
    return args


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
    # Use a distinct name even when the downloaded source is already WAV.
    wav = audio_path.with_name(f"{audio_path.stem}.bpm.wav")
    start = min(60, int(video_duration // 3)) if video_duration else 30
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-ss", str(start), "-i", str(audio_path),
         "-t", "60", "-ac", "1", "-ar", "22050", str(wav)],
        check=True,
    )

    # Detector 1: deeprhythm (note: needs WAV, doesn't read webm/m4a).
    try:
        bpm_dr = fit_to_range(float(deeprhythm_model().predict(str(wav))))
    except Exception:
        bpm_dr = None

    # Detector 2: librosa.
    y, sr = librosa.load(str(wav), sr=None, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm_lr = fit_to_range(float(np.atleast_1d(tempo)[0]))

    if bpm_dr is None and bpm_lr is None:
        return None, None, False
    if bpm_lr is None:
        return bpm_dr, None, False   # deeprhythm alone: reliable
    if bpm_dr is None:
        return bpm_lr, None, True    # librosa alone: better to confirm
    if abs(bpm_dr - bpm_lr) <= TOLERANCE_BPM:
        return bpm_dr, None, False   # two detectors agree
    return bpm_dr, bpm_lr, True


# Krumhansl-Schmuckler pitch-class profiles, ordered C through B. librosa
# provides the chromagram, not the final 24-way key classifier, so we compare
# its full-track harmonic chroma with every rotation of these profiles.
KRUMHANSL_MAJOR = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
KRUMHANSL_MINOR = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)
PITCH_NAMES = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")


def estimate_key_from_chroma(chroma):
    """Returns (key, best correlation, best-minus-second margin).

    This small pure function is kept separate so the 24-way classifier can be
    unit-tested without loading audio or either detector's native dependency.
    """
    chroma = np.asarray(chroma, dtype=float)
    if chroma.ndim == 2:
        chroma = np.mean(chroma, axis=1)
    if chroma.shape != (12,) or not np.isfinite(chroma).all() or not np.any(chroma):
        return None, None, None

    scores = []
    for tonic, name in enumerate(PITCH_NAMES):
        for profile, suffix in ((KRUMHANSL_MAJOR, ""), (KRUMHANSL_MINOR, "m")):
            score = float(np.corrcoef(chroma, np.roll(profile, tonic))[0, 1])
            if np.isfinite(score):
                scores.append((score, f"{name}{suffix}"))
    if not scores:
        return None, None, None
    scores.sort(reverse=True)
    best_score, key = scores[0]
    margin = best_score - scores[1][0] if len(scores) > 1 else None
    return normalize_key(key), best_score, margin


def measure_key_essentia(audio_path):
    """Estimates the global key with Essentia's EDM-specific bgate profile."""
    try:
        import essentia.standard as essentia

        audio = essentia.MonoLoader(filename=str(audio_path), sampleRate=22050)()
        tonic, scale, strength = essentia.KeyExtractor(
            sampleRate=22050,
            profileType="bgate",
            hpcpSize=36,
        )(audio)
        key = normalize_key(f"{tonic} {scale}")
        return key, float(strength) if key else None
    except Exception:
        return None, None


def measure_key_librosa(audio_path):
    """Estimates global key from full-track harmonic CQT chroma."""
    try:
        y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
        if not np.any(np.abs(y) > 1e-7):
            return None, None
        harmonic = librosa.effects.harmonic(y, margin=8)
        chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr)
        key, correlation, _margin = estimate_key_from_chroma(chroma)
        return key, correlation
    except Exception:
        return None, None


def measure_key(audio_path):
    """Runs both global-key detectors and applies the consensus policy."""
    key_es, strength_es = measure_key_essentia(audio_path)
    key_lr, strength_lr = measure_key_librosa(audio_path)
    estimates = []
    if key_es:
        estimates.append(KeySourceEstimate("essentia", key_es, strength_es))
    if key_lr:
        estimates.append(KeySourceEstimate("librosa", key_lr, strength_lr))

    if key_es and key_lr:
        if key_es == key_lr:
            return key_es, None, False, strength_es, estimates
        return key_es, key_lr, True, strength_es, estimates
    if key_es:
        return key_es, None, True, strength_es, estimates
    if key_lr:
        return key_lr, None, True, strength_lr, estimates
    return None, None, False, None, estimates


def measure_audio(audio_path, video_duration, *, need_bpm=True, need_key=True):
    """Measures only the missing fields, avoiding unnecessary heavy work."""
    result = AudioAnalysis()
    if need_bpm:
        bpm, bpm_alt, bpm_doubtful = measure_bpm(audio_path, video_duration)
        result.bpm = bpm
        result.bpm_alt = bpm_alt
        result.bpm_doubtful = bpm_doubtful
    if need_key:
        key, key_alt, key_doubtful, key_strength, estimates = measure_key(audio_path)
        result.key = key
        result.key_alt = key_alt
        result.key_doubtful = key_doubtful
        result.key_strength = key_strength
        result.key_estimates = estimates
    return result


def download_and_measure(video, tmpdir, *, need_bpm=True, need_key=True):
    """Downloads audio, measures BPM and key, then deletes temporary files."""
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
        return measure_audio(
            audio_path,
            video.get("duration"),
            need_bpm=need_bpm,
            need_key=need_key,
        )
    finally:
        # delete the audio as soon as we measure it (or what's left
        # of a failed download)
        for file in Path(tmpdir).iterdir():
            file.unlink()


def analyze_track(
    artist,
    title,
    target_duration,
    tmpdir,
    catno=None,
    *,
    need_bpm=True,
    need_key=True,
):
    """Searches for the track (Bandcamp first, YouTube and SoundCloud if not),
    downloads the best candidate, and returns (AudioAnalysis, detail).

    ``need_bpm`` and ``need_key`` decide which result makes a candidate useful.
    This matters when a track already has BPM but still needs a key: a candidate
    that only yields tempo must not stop the search prematurely.
    """
    queries = build_queries(artist, title, catno)

    def found_needed(result):
        return ((need_bpm and result.bpm is not None)
                or (need_key and result.key is not None))

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
                result = download_and_measure(
                    video, tmpdir, need_bpm=need_bpm, need_key=need_key
                )
            except Exception as e:
                reasons.append(f"{name}: {summarize_error(e)}")
                continue
            if not found_needed(result):
                reasons.append(f"{name}: couldn't measure the missing BPM/key")
                continue
            return result, f"{video.get('title', '')} [{name}]"

    # Rescue pass: no one passed the complete filter, but these candidates
    # match title and artist and only fail on duration. It's almost always
    # another edition of the same song (album version vs 12", or misEntered
    # Discogs duration), and tempo doesn't change between editions. But the
    # result is ALWAYS doubtful, with both durations noted, so you have
    # the final say in the editor.
    rescues.sort(key=lambda pair: abs(pair[1]["duration"] - target_duration))
    for name, video in rescues[:2]:
        try:
            result = download_and_measure(
                video, tmpdir, need_bpm=need_bpm, need_key=need_key
            )
        except Exception as e:
            reasons.append(f"{name}: {summarize_error(e)}")
            continue
        if not found_needed(result):
            reasons.append(f"{name}: couldn't measure the missing BPM/key")
            continue
        result = replace(
            result,
            bpm_doubtful=result.bpm is not None,
            key_doubtful=result.key is not None,
        )
        detail = (f"{video.get('title', '')} [{name}; note: lasts "
                  f"{format_duration(video['duration'])} but Discogs says "
                  f"{format_duration(target_duration)} — different edition?]")
        return result, detail

    return AudioAnalysis(), " | ".join(reasons)


def main():
    args = parse_arguments()
    process_all = args.all
    limit = args.limit

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT tracks.id, tracks.release_id, tracks.title, tracks.duration_display,
               tracks.bpm, tracks.key,
               COALESCE(tracks.artist, releases.artist) AS artist,
               releases.catno
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE (tracks.bpm IS NULL OR tracks.key IS NULL)
          AND (? OR NOT EXISTS (
              SELECT 1 FROM workflow_steps
              WHERE workflow_steps.release_id = releases.release_id
                AND step = 'analyze'
          ))
        ORDER BY releases.artist, releases.title, tracks.id
        """,
        (int(process_all),),
    )
    all_pending = cursor.fetchall()
    pending = all_pending
    if limit:
        pending = pending[:limit]

    candidate_releases = {
        row["release_id"]
        for row in cursor.execute(
            "SELECT release_id FROM releases WHERE ?"
            " OR NOT EXISTS (SELECT 1 FROM workflow_steps"
            "                WHERE workflow_steps.release_id = releases.release_id"
            "                  AND step = 'analyze')",
            (int(process_all),),
        )
    }

    print(f"Tracks to analyze: {len(pending)}")
    if not pending:
        for release_id in candidate_releases:
            mark_workflow_step(conn, release_id, "analyze")
        conn.commit()
        conn.close()
        print("Nothing new to analyze. Use --all to revisit old missing BPMs/keys.")
        return
    print("(this downloads audio and measures BPM/key here; full-track key analysis")
    print(" can take longer than 30s per track,")
    print(" you can stop with Ctrl+C and resume later)")
    if args.pace:
        print(f"Pace: {args.pace:g}s between tracks.\n")
    else:
        print("Pace: no delay between tracks.\n")

    bpm_found = 0
    bpm_doubtful = 0
    keys_found = 0
    keys_doubtful = 0
    attempted = set()
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pending, start=1):
            label = f"[{i}/{len(pending)}] {row['artist']} - {row['title']}"
            need_bpm = row["bpm"] is None
            need_key = row["key"] is None
            try:
                result, detail = analyze_track(
                    row["artist"], row["title"],
                    parse_duration(row["duration_display"]), tmpdir,
                    row["catno"],
                    need_bpm=need_bpm,
                    need_key=need_key,
                )
            except KeyboardInterrupt:
                print("\nStopped. What was analyzed is saved.")
                break
            except Exception as e:
                print(f"{label}\n   -> error, continuing: {e}")
                continue

            updates = []
            if need_bpm and result.bpm is not None:
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'youtube',"
                    " bpm_alt = ?, bpm_needs_review = ?, bpm_verified = 0 WHERE id = ?",
                    (result.bpm, result.bpm_alt, int(result.bpm_doubtful), row["id"]),
                )
                record_bpm_source(conn, row["id"], "youtube", result.bpm, detail)
                bpm_found += 1
                bpm_doubtful += int(result.bpm_doubtful)
                if result.bpm_doubtful and result.bpm_alt is not None:
                    warning = f"; other detector {result.bpm_alt:g}?"
                elif result.bpm_doubtful:
                    warning = "; review"
                else:
                    warning = ""
                updates.append(f"{result.bpm:g} BPM{warning}")

            if need_key and result.key is not None:
                cursor.execute(
                    "UPDATE tracks SET key = ?, key_source = 'audio', key_alt = ?,"
                    " key_needs_review = ?, key_verified = ?, key_strength = ?"
                    " WHERE id = ?",
                    (
                        result.key,
                        result.key_alt,
                        int(result.key_doubtful),
                        int(not result.key_doubtful),
                        result.key_strength,
                        row["id"],
                    ),
                )
                for estimate in result.key_estimates:
                    record_key_source(
                        conn,
                        row["id"],
                        estimate.source,
                        estimate.key,
                        estimate.strength,
                        detail,
                    )
                keys_found += 1
                keys_doubtful += int(result.key_doubtful)
                key_text = result.key
                if result.key_alt:
                    key_text += f"; other detector {result.key_alt}?"
                elif result.key_doubtful:
                    key_text += "; review"
                else:
                    key_text += "; detectors agree"
                updates.append(f"key {key_text}")

            if updates:
                print(f"{label} -> {', '.join(updates)}\n   (measured from: {detail})")
            else:
                print(f"{label}\n   -> {detail}")

            attempted.add(row["id"])
            conn.commit()

            # Pause between tracks to reduce source rate limiting. Do not make
            # the user wait after the final track in this batch.
            if args.pace and i < len(pending):
                time.sleep(args.pace)

    required_by_release = {}
    for row in all_pending:
        required_by_release.setdefault(row["release_id"], set()).add(row["id"])
    for release_id in candidate_releases:
        if required_by_release.get(release_id, set()) <= attempted:
            mark_workflow_step(conn, release_id, "analyze")
    conn.commit()
    conn.close()
    print("\n" + "=" * 50)
    print(f"BPM measured: {bpm_found} ({bpm_doubtful} need review).")
    print(f"Keys measured: {keys_found} ({keys_doubtful} need review).")
    if bpm_doubtful or keys_doubtful:
        print("Review detector disagreements in the editor: python edit_bpm.py")
    else:
        print("The detectors agreed on every locally measured key.")
    if bpm_found:
        print("Fallback BPMs still need your validation in: python edit_bpm.py")


if __name__ == "__main__":
    main()
