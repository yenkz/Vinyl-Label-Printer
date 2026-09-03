"""
render_labels.py — STEP 7

Generates ONE image per vinyl record (not one per track), with the cover,
label, and release date in the header, and a table of all its tracks:
position, title, duration, BPM, and tonality (key, in Camelot notation: "8A").
The width is always 696px (the 62mm of the roll), and height varies based on
how many tracks the record has.

The cover is downloaded by fetch_discogs.py (step 1), and if Discogs didn't
have it, by Bandcamp/Spotify steps; if a record doesn't have one, the header
comes out text-only, like before.

BPM and key are in bold because that's what you'll be reading in the dark
in the booth.

A label is generated only when every track on the record has a BPM validated
in edit_bpm.py. Records with missing or unvalidated BPMs remain pending and
will be picked up by the next run after you validate them. Every run also
compares the current database-backed rendering with the image on disk: changed
labels are replaced, while identical ones are left untouched.

Superseded pending filenames are moved to labels_output/obsolete/. Confirmed
files under labels_output/printed/ are retained as immutable print history.

Images are saved in the labels_output/ folder, with names like
"Artist - Record (id).png", ready for print_labels.py to send to the printer.

How to run it:
    python -m vinyl_labels render              # generate labels for new records
    python -m vinyl_labels render --all        # regenerate all labels
    python -m vinyl_labels render aphex        # filter records
    python -m vinyl_labels render aphex --view # also open in Preview
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from vinyl_labels import config
from vinyl_labels.common import to_camelot
from vinyl_labels.db import get_connection, init_db, mark_workflow_step
from vinyl_labels.paths import project_path

OUTPUT_DIR = project_path(config.OUTPUT_DIR)

# --- Label design measurements (in pixels, at 300dpi) ---
MARGIN = 16
HEADER_LINE = 44   # height of each header row (artist / record)
META_LINE = 32     # height of small rows (label / date)
TITLES_ROW = 30    # row with column names (DUR/BPM/KEY)
ROW_HEIGHT = 46
FOOTER_MARGIN = 16
COVER_PX = 170      # side of cover in header


def load_fonts():
    """Tries to load the configured font; if it doesn't exist on this
    computer, uses Pillow's default (less pretty, but it works)."""
    try:
        bold = ImageFont.truetype(config.FONT_PATH_BOLD, 34)
        text = ImageFont.truetype(config.FONT_PATH, 26)
        bpm_bold = ImageFont.truetype(config.FONT_PATH_BOLD, 26)
        meta = ImageFont.truetype(config.FONT_PATH, 22)
    except OSError:
        print("Warning: couldn't find configured font in FONT_PATH, using default.")
        bold = text = bpm_bold = meta = ImageFont.load_default()
    return bold, text, bpm_bold, meta


def format_date(release):
    """Vinyl release date, clean. Discogs sometimes sends "2005-00-00" when
    it only knows the year; we remove the "-00". If no date, use year only."""
    date = re.sub(r"(-00)+$", "", (release["released"] or "").strip())
    return date or (str(release["year"]) if release["year"] else "")


def load_cover(release):
    """Opens the cover downloaded by enrich_spotify.py and converts it to
    pure black and white with halftone — exactly how the thermal printer will
    print it, so what you see on screen is what comes out on paper."""
    if not release["cover_path"]:
        return None
    path = project_path(release["cover_path"])
    if not path.exists():
        return None
    try:
        cover = Image.open(path).resize((COVER_PX, COVER_PX)).convert("L").convert("1")
    except OSError:
        return None
    return cover.convert("RGB")


def truncate_text(draw, text, font, max_width):
    """Cuts text with '...' if it doesn't fit in available width."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "...", font=font) > max_width:
        text = text[:-1]
    return text + "..."


def file_name(release):
    """Creates a readable and valid file name, like
    "Aphex Twin - Selected Ambient Works (12345).png"."""
    base = f"{release['artist']} - {release['title']}"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return f"{base[:120]} ({release['release_id']}).png"


# Keep the release ID as the durable identity of a rendered artifact. Artist
# and title make filenames pleasant to browse, but Discogs may correct either
# one after a label has already been rendered.
RELEASE_ID = re.compile(r"\((?P<release_id>[1-9]\d*)\)\.png\Z", re.IGNORECASE)


def release_id_from_path(path):
    """Returns the positive release ID encoded at the end of a label name."""
    match = RELEASE_ID.search(Path(path).name)
    return int(match.group("release_id")) if match else None


def artifacts_for_release(directory, release_id):
    """Finds direct-child PNG artifacts belonging to ``release_id``."""
    directory = Path(directory)
    return sorted(
        path
        for path in directory.glob("*.png")
        if release_id_from_path(path) == int(release_id)
    )


def unique_artifact_path(directory, name):
    """Returns a non-destructive destination while retaining the ID suffix."""
    directory = Path(directory)
    candidate = directory / name
    if not candidate.exists():
        return candidate

    match = RELEASE_ID.search(name)
    if not match:
        stem = Path(name).stem
        suffix = Path(name).suffix
        for number in range(2, sys.maxsize):
            candidate = directory / f"{stem} [revision {number}]{suffix}"
            if not candidate.exists():
                return candidate

    prefix = name[:match.start()].rstrip()
    release_suffix = match.group(0)
    for number in range(2, sys.maxsize):
        candidate = directory / f"{prefix} [revision {number}] {release_suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Couldn't choose a unique artifact name for {name}")


def archive_pending_artifacts(output_dir, release_id, keep=None):
    """Moves obsolete pending versions out of the printer's input directory.

    Nothing is deleted, and ``printed/`` is deliberately never inspected or
    modified: it is the physical print history. The returned paths are useful
    for reporting and tests.
    """
    output_dir = Path(output_dir)
    keep = Path(keep) if keep is not None else None
    obsolete = [
        path
        for path in artifacts_for_release(output_dir, release_id)
        if keep is None or path != keep
    ]
    if not obsolete:
        return []

    archive_dir = output_dir / "obsolete"
    archive_dir.mkdir(exist_ok=True)
    archived = []
    for path in obsolete:
        destination = unique_artifact_path(archive_dir, path.name)
        path.rename(destination)
        archived.append(destination)
    return archived


def archive_orphaned_pending(output_dir, current_release_ids):
    """Archives pending labels whose release is no longer in the collection."""
    current_release_ids = {int(release_id) for release_id in current_release_ids}
    archived = []
    for path in sorted(Path(output_dir).glob("*.png")):
        release_id = release_id_from_path(path)
        if release_id is not None and release_id not in current_release_ids:
            archived.extend(archive_pending_artifacts(output_dir, release_id))
    return archived


def render_release(release, tracks, font_title, font_text, font_bpm, font_meta):
    cover = load_cover(release)
    label = " · ".join(filter(None, (release["label"], release["catno"])))
    date = format_date(release)

    # --- Header: cover on the left (if any) and next to it the artist
    # (bold), the record, the label, and the release date ---
    text_x = MARGIN + COVER_PX + 16 if cover else MARGIN
    text_y = 14 if cover else 6
    y = text_y + HEADER_LINE * 2
    if label:
        y += META_LINE
    if date:
        y += META_LINE
    header_height = max(y + 12, COVER_PX + 24 if cover else 0)

    total_height = header_height + TITLES_ROW + ROW_HEIGHT * len(tracks) + FOOTER_MARGIN
    img = Image.new("RGB", (config.LABEL_WIDTH_PX, total_height), "white")
    draw = ImageDraw.Draw(img)

    if cover:
        img.paste(cover, (MARGIN, 12))
    usable_width = config.LABEL_WIDTH_PX - text_x - MARGIN

    y = text_y
    artist_text = truncate_text(draw, release["artist"], font_title, usable_width)
    draw.text((text_x, y), artist_text, font=font_title, fill="black")
    y += HEADER_LINE
    release_text = truncate_text(draw, release["title"], font_text, usable_width)
    draw.text((text_x, y), release_text, font=font_text, fill="black")
    y += HEADER_LINE
    if label:
        draw.text((text_x, y), truncate_text(draw, label, font_meta, usable_width), font=font_meta, fill="black")
        y += META_LINE
    if date:
        draw.text((text_x, y), date, font=font_meta, fill="black")
    draw.line(
        [(MARGIN, header_height - 8), (config.LABEL_WIDTH_PX - MARGIN, header_height - 8)],
        fill="black",
        width=2,
    )

    # --- Columns: position | track title | duration | BPM | key ---
    col_position_x = MARGIN
    col_title_x = MARGIN + 60
    col_duration_x = config.LABEL_WIDTH_PX - 246
    col_bpm_x = config.LABEL_WIDTH_PX - 156
    col_key_x = config.LABEL_WIDTH_PX - 76

    # Column names, small, below the line. KEY only if some track has it
    # (on non-electronic records it would just be noise).
    draw.text((col_duration_x, header_height + 2), "DUR", font=font_meta, fill="black")
    draw.text((col_bpm_x, header_height + 2), "BPM", font=font_meta, fill="black")
    if any(t["key"] for t in tracks):
        draw.text((col_key_x, header_height + 2), "KEY", font=font_meta, fill="black")

    y = header_height + TITLES_ROW
    for track in tracks:
        draw.text((col_position_x, y + 8), track["position"] or "", font=font_text, fill="black")

        title_max_width = col_duration_x - col_title_x - 10
        # On "Various" records each track can have its own artist (Discogs
        # stores it per track); if we have it, we show it before the title
        # so that info isn't lost on the label.
        track_text = track["title"] or ""
        if track["artist"]:
            track_text = f"{track['artist']} – {track_text}"
        track_title = truncate_text(draw, track_text, font_text, title_max_width)
        draw.text((col_title_x, y + 8), track_title, font=font_text, fill="black")

        draw.text((col_duration_x, y + 8), track["duration_display"] or "--:--", font=font_text, fill="black")

        if track["bpm"]:
            bpm_text = str(round(track["bpm"]))
            # The asterisk marks doubtful BPM (the detectors didn't agree
            # and you haven't confirmed it yet).
            if track["bpm_needs_review"]:
                bpm_text += "*"
        else:
            bpm_text = "?"
        draw.text((col_bpm_x, y + 8), bpm_text, font=font_bpm, fill="black")

        # The key in Camelot ("8A"). No "?" when missing: most non-electronic
        # records will never have it.
        if track["key"]:
            draw.text((col_key_x, y + 8), to_camelot(track["key"]), font=font_bpm, fill="black")

        y += ROW_HEIGHT

    return img


def same_image(rendered, path):
    """True when an existing PNG has exactly the pixels we would render now.

    Comparing pixels instead of timestamps catches every visible database
    change from edit_bpm.py (BPM, key, doubtful marker), while also detecting
    layout/font/cover changes. A missing, unreadable, or corrupt image is stale.
    """
    try:
        with Image.open(path) as existing:
            existing.load()
            return (
                existing.mode == rendered.mode
                and existing.size == rendered.size
                and existing.tobytes() == rendered.tobytes()
            )
    except (OSError, ValueError):
        return False


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels render",
        description="Render validated vinyl labels.",
    )
    parser.add_argument("filter", nargs="?", help="artist/record text to match")
    parser.add_argument("--view", action="store_true", help="open newly rendered labels")
    parser.add_argument("--all", action="store_true", help="force regeneration")
    return parser.parse_args(arguments)


def main(arguments=None):
    args = parse_arguments(arguments)
    open_preview = args.view
    process_all = args.all
    filter_text = (args.filter or "").lower()

    OUTPUT_DIR.mkdir(exist_ok=True)

    init_db()  # in case you haven't run any other step yet
    conn = get_connection()
    cursor = conn.cursor()
    # We inspect every record because an already-rendered label may have changed
    # in edit_bpm.py. Pixel comparison below keeps this incremental: unchanged
    # files are not rewritten. A named filter or --all remains an explicit force.
    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()

    if not releases:
        print("Your collection is empty. Run first: python -m vinyl_labels fetch")
        conn.close()
        return 1

    orphaned = archive_orphaned_pending(
        OUTPUT_DIR, (release["release_id"] for release in releases)
    )
    if orphaned:
        print(
            f"Archived {len(orphaned)} obsolete pending label(s) for releases "
            "no longer in the collection."
        )

    if filter_text:
        releases = [r for r in releases if filter_text in f"{r['artist']} {r['title']}".lower()]
        if not releases:
            print(f"No record in your collection contains '{filter_text}'.")
            conn.close()
            return 1

    font_title, font_text, font_bpm, font_meta = load_fonts()

    generated = 0
    updated = 0
    unchanged = 0
    waiting_for_validation = 0
    generated_paths = []
    force_render = process_all or bool(filter_text)
    for release in releases:
        cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY sort_order, id",
            (release["release_id"],),
        )
        tracks = cursor.fetchall()
        if not tracks:
            continue

        validated = sum(
            1 for track in tracks
            if track["bpm"] is not None and track["bpm_verified"]
        )
        if validated != len(tracks):
            archived = archive_pending_artifacts(OUTPUT_DIR, release["release_id"])
            if archived:
                print(
                    f"Archived {len(archived)} no-longer-printable pending "
                    f"label(s) for release {release['release_id']}."
                )
            waiting_for_validation += 1
            print(
                f"Waiting for validation: {release['artist']} - {release['title']} "
                f"({validated}/{len(tracks)} tracks validated)"
            )
            # Do not mark the render workflow step: once validation is complete,
            # a normal `make render` must see this release again.
            continue

        name = file_name(release)
        output_path = OUTPUT_DIR / name
        # A metadata rename changes the readable filename. Retire every older
        # pending name for this ID before writing the canonical one, otherwise
        # print_labels.py could see two copies of the same record.
        archived = archive_pending_artifacts(
            OUTPUT_DIR, release["release_id"], keep=output_path
        )
        if archived:
            print(
                f"Archived {len(archived)} superseded pending label(s) for "
                f"release {release['release_id']}."
            )

        img = render_release(release, tracks, font_title, font_text, font_bpm, font_meta)
        pending_existing = output_path if output_path.exists() else None
        printed_versions = artifacts_for_release(
            OUTPUT_DIR / "printed", release["release_id"]
        )
        existing_versions = (
            ([pending_existing] if pending_existing else []) + printed_versions
        )
        matching_existing = next(
            (path for path in existing_versions if same_image(img, path)), None
        )
        if not force_render and matching_existing:
            # If the current pixels match print history but a different image
            # is still pending under the canonical name, it is stale and must
            # not remain eligible for printing.
            if pending_existing and matching_existing != pending_existing:
                archived = archive_pending_artifacts(
                    OUTPUT_DIR, release["release_id"]
                )
                if archived:
                    print(
                        f"Archived {len(archived)} stale pending label(s) for "
                        f"release {release['release_id']}."
                    )
            mark_workflow_step(conn, release["release_id"], "render")
            conn.commit()
            unchanged += 1
            continue

        img.save(output_path)
        mark_workflow_step(conn, release["release_id"], "render")
        conn.commit()
        generated_paths.append(output_path)
        generated += 1
        if existing_versions:
            updated += 1
            print(f"Updated: {name}")
        else:
            print(f"Generated: {name}")

    conn.close()
    print(
        f"\nDone. {generated} labels written in {OUTPUT_DIR}/ "
        f"({updated} updated, {generated - updated} new, {unchanged} unchanged)."
    )
    if waiting_for_validation:
        print(
            f"{waiting_for_validation} records are still waiting for 100% BPM "
            "validation in `make edit`."
        )

    if open_preview and generated_paths:
        # Open the images in Preview (Mac) to check them before
        # wasting a label.
        subprocess.run(["open", *map(str, generated_paths)])

    if generated:
        print("Next step: python -m vinyl_labels print --test  (dry run)")
        print("        or: python -m vinyl_labels print         (print)")
    elif waiting_for_validation:
        print("Next step: make edit  (validate every track, then run make render again)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
