"""Pure matching helpers for local-audio search results.

This module has no downloader, detector, database, or network dependencies, so
the identity and duration policy can be tested independently of audio analysis.
"""

import difflib

# How much a candidate duration can differ from Discogs while still counting as
# the same track: 20 seconds or 12%, whichever is larger.
TOLERANCE_SECONDS = 20
TOLERANCE_PERCENTAGE = 0.12

# Words that do not distinguish one track title from another.
EMPTY_WORDS = {"the", "and", "you", "your", "feat", "with", "mix", "original"}

# Signs that a result is a whole release or preview rather than one track.
# Such results are excluded from duration-rescue matching because they may mix
# several songs and tempos.
COMPILATION_WORDS = {
    "ep",
    "lp",
    "va",
    "inc",
    "incl",
    "minimix",
    "megamix",
    "preview",
    "previews",
    "snippet",
    "snippets",
    "sampler",
    "showreel",
    "teaser",
}

# Threshold for character-by-character partial title comparison.
FUZZY_THRESHOLD = 0.75


def words(text):
    """Convert a title or credit to its comparable words."""
    clean = "".join(character.lower() if character.isalnum() else " " for character in text)
    return {part for part in clean.split() if len(part) > 2 and part not in EMPTY_WORDS}


def split_artists(artist):
    """Split a composite Discogs artist credit into independently searchable names."""
    if artist.lower() in ("various", "unknown"):
        return []
    return [part.strip() for part in artist.split(" / ") if part.strip()]


def artist_tokens(artist):
    """Return comparable words from every artist in a composite credit."""
    tokens = set()
    for part in split_artists(artist):
        tokens |= words(part)
    return tokens


def build_queries(artist, title, catno):
    """Build one query per credited artist plus an optional catalog query."""
    queries = [f"{part} {title}" for part in split_artists(artist)] or [title]
    if catno:
        queries.append(f"{catno} {title}")
    return queries


def seems_compilation(video_title):
    """Return whether a title appears to describe a release or multi-track preview."""
    clean = "".join(
        character.lower() if character.isalnum() else " " for character in video_title
    )
    return bool(set(clean.split()) & COMPILATION_WORDS)


def compact(text):
    """Remove spacing and punctuation for fuzzy character comparison."""
    return "".join(character.lower() for character in text if character.isalnum())


def partial_similarity(first, second):
    """Measure how well the shorter compacted text fits inside the longer one."""
    first, second = compact(first), compact(second)
    if not first or not second:
        return 0.0
    short, long = (
        (first, second) if len(first) <= len(second) else (second, first)
    )
    best = 0.0
    for index in range(len(long) - len(short) + 1):
        best = max(
            best,
            difflib.SequenceMatcher(
                None, short, long[index : index + len(short)]
            ).ratio(),
        )
        if best == 1.0:
            break
    return best


def title_matches(track_title, candidate_title):
    """Return whether a search-result title appears to name the requested track."""
    target = words(track_title)
    candidate_words = words(candidate_title)
    if target and len(target & candidate_words) / len(target) >= 0.5:
        return True
    return partial_similarity(track_title, candidate_title) >= FUZZY_THRESHOLD


def select_videos(candidates, artist, track_title, target_duration):
    """Split candidates into approved and duration-rescue lists.

    Both lists are sorted from closest to furthest duration. Approved results
    match title, artist, and duration. Rescue results match title and artist but
    differ in duration and are restricted to plausible single-track uploads.
    """
    tokens = artist_tokens(artist)

    approved = []
    rescue = []
    for video in candidates:
        duration = video.get("duration")
        if not duration:
            continue

        video_title = video.get("title", "")
        if not title_matches(track_title, video_title):
            continue

        channel = video.get("uploader") or video.get("channel") or ""
        if tokens and not tokens & (words(video_title) | words(channel)):
            continue

        if target_duration:
            tolerance = max(
                TOLERANCE_SECONDS,
                target_duration * TOLERANCE_PERCENTAGE,
            )
            difference = abs(duration - target_duration)
            if difference <= tolerance:
                approved.append((difference, video))
            elif 120 <= duration <= 900 and not seems_compilation(video_title):
                rescue.append((difference, video))
        elif 120 <= duration <= 900:
            approved.append((0, video))
    approved.sort(key=lambda pair: pair[0])
    rescue.sort(key=lambda pair: pair[0])
    return [video for _, video in approved], [video for _, video in rescue]
