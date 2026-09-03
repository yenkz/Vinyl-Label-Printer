"""Pure Soulseek matching and library-file helpers.

The slskd command owns network orchestration and transfer state. This module
owns the provider-independent rules for recognizing candidates, ranking them,
constructing safe library paths, and tagging completed audio files.
"""

import re
import shutil
from pathlib import Path

from . import config
from .common import ascii_fold, looks_similar, normalize
from .paths import PROJECT_ROOT

# A leading track number or vinyl position on a shared file ("A1 ", "01 - ",
# "1.") says nothing about which song it is, so drop it before comparison.
LEADING_INDEX = re.compile(r"^\s*([A-F]?\d{1,2}|[A-F])[\s._)\-]+", re.IGNORECASE)


def remote_basename(remote_path):
    """Return a basename from Soulseek's Windows-style remote path."""
    return remote_path.replace("\\", "/").rsplit("/", 1)[-1]


def track_stem(remote_path):
    """Return a remote filename without folder, extension, or track index."""
    base = remote_basename(remote_path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return LEADING_INDEX.sub("", stem).strip()


def safe_name(text):
    """Return a non-empty file/folder name without unsafe path characters."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text or "")
    return text.strip().strip(".") or "Unknown"


def clean_query(text):
    """Fold accents and replace punctuation for Soulseek word matching."""
    return re.sub(
        r"\s+", " ", re.sub(r"[^\w\s]", " ", ascii_fold(text or ""))
    ).strip()


def artist_names(*credits):
    """Return distinct usable artists from composite Discogs credits."""
    names = []
    for credit in credits:
        for name in (credit or "").split(" / "):
            name = name.strip()
            if (
                name
                and not name.lower().startswith(("various", "unknown"))
                and name not in names
            ):
                names.append(name)
    return names


def mentions(remote_path, artists):
    """Whether a shared path mentions at least one credited artist."""
    path = normalize(remote_path)
    return any(normalize(artist) in path for artist in artists)


def format_rank(file_info):
    """Preference score for an audio candidate; -1 means unsupported."""
    extension = (file_info.get("extension") or "").lower().lstrip(".")
    if not extension:
        base = remote_basename(file_info.get("filename", ""))
        extension = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if extension in ("aiff", "aif"):
        return 5
    if extension == "flac":
        return 4
    if extension == "wav":
        return 3
    if extension == "mp3":
        return 2 if (file_info.get("bitRate") or 0) >= 320 else 1
    return -1


def candidate_score(response, file_info, target_duration=0):
    """Return the exact preference tuple used to order matching files."""
    duration_penalty = (
        abs((file_info.get("length") or 0) - target_duration)
        if target_duration
        else 0
    )
    return (
        format_rank(file_info),
        int(response.get("hasFreeUploadSlot", False)),
        -duration_penalty,
        response.get("uploadSpeed", 0),
        -response.get("queueLength", 0),
    )


def rank_candidates(
    responses,
    track_title,
    credited_artists=(),
    target_duration=0,
    require_artist=False,
):
    """Filter and rank search responses, keeping one best file per peer.

    Returns ``(ranked, saw_unconfirmed)``. ``ranked`` contains
    ``(file_info, username, score)`` tuples. ``saw_unconfirmed`` records that
    a title matched during a title-only search but the path named no credited
    artist.
    """
    best_per_user = {}
    unconfirmed = False
    for response in responses:
        for file_info in response.get("files", []):
            if file_info.get("isLocked") or format_rank(file_info) < 0:
                continue
            if not looks_similar(track_stem(file_info["filename"]), track_title):
                continue
            if require_artist and not mentions(file_info["filename"], credited_artists):
                unconfirmed = True
                continue
            score = candidate_score(response, file_info, target_duration)
            username = response["username"]
            if username not in best_per_user or score > best_per_user[username][0]:
                best_per_user[username] = (score, file_info)

    ranked = sorted(best_per_user.items(), key=lambda item: item[1][0], reverse=True)
    return [
        (file_info, username, score)
        for username, (score, file_info) in ranked
    ], unconfirmed


def cover_bytes(release):
    """Return the locally cached release cover, if it still exists."""
    if not release["cover_path"]:
        return None
    path = PROJECT_ROOT / release["cover_path"]
    return path.read_bytes() if path.exists() else None


def tag_file(path, track, release):
    """Write release metadata and cover art to a completed audio file."""
    extension = path.suffix.lower().lstrip(".")
    cover = cover_bytes(release)
    artist = track["artist"] or release["artist"] or ""

    if extension == "flac":
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
            picture.type = 3
            picture.mime = "image/jpeg"
            picture.data = cover
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()
        return

    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPUB, TRCK

    if extension in ("aiff", "aif"):
        from mutagen.aiff import AIFF as Container
    elif extension == "wav":
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
        tags.setall(
            "APIC",
            [APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover)],
        )
    audio.save()


def locate_download(file_info, track):
    """Find a completed slskd file by exact name, then fuzzy title."""
    root = Path(config.SLSKD_DOWNLOADS_DIR).expanduser()
    if not root.exists():
        return None
    base = remote_basename(file_info["filename"])
    exact = [path for path in root.rglob(base) if path.is_file()]
    if exact:
        return max(exact, key=lambda path: path.stat().st_mtime)
    extension = (
        file_info.get("extension")
        or (base.rsplit(".", 1)[-1] if "." in base else "")
    ).lower().lstrip(".")
    if not extension:
        return None
    fuzzy = [
        path
        for path in root.rglob(f"*.{extension}")
        if path.is_file()
        and looks_similar(LEADING_INDEX.sub("", path.stem).strip(), track["title"])
    ]
    return max(fuzzy, key=lambda path: path.stat().st_mtime) if fuzzy else None


def library_path(music_dir, extension, track, release):
    """Build the safe final library path without touching the filesystem."""
    folder = f"{release['artist']} - {release['title']}"
    if release["catno"]:
        folder += f" ({release['catno']})"
    prefix = f"{track['position']} " if track["position"] else ""
    filename = safe_name(prefix + (track["title"] or "Untitled"))
    return Path(music_dir).expanduser() / safe_name(folder) / f"{filename}{extension}"


def place_and_tag(src, track, release):
    """Move a download into the library and tag it, returning its final path."""
    destination = library_path(config.MUSIC_DIR, src.suffix.lower(), track, release)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(destination))
    try:
        tag_file(destination, track, release)
    except Exception as error:
        print(f"      (couldn't write tags: {error})")
    return destination
