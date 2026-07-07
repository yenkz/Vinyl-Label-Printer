"""
render_labels.py — STEP 4

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

Images are saved in the labels_output/ folder, with names like
"Artist - Record (id).png", ready for print_labels.py to send to the printer.

How to run it:
    python render_labels.py              # generate ALL labels
    python render_labels.py aphex        # only records containing "aphex"
    python render_labels.py aphex --view # also open them in Preview
"""

import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config
from common import to_camelot
from db import get_connection, init_db

OUTPUT_DIR = Path(__file__).parent / config.OUTPUT_DIR

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
    path = Path(__file__).parent / release["cover_path"]
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


def main():
    arguments = sys.argv[1:]
    open_preview = "--view" in arguments
    filter_text = next((a.lower() for a in arguments if not a.startswith("--")), "")

    OUTPUT_DIR.mkdir(exist_ok=True)

    init_db()  # in case you haven't run any other step yet
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

    font_title, font_text, font_bpm, font_meta = load_fonts()

    generated = 0
    generated_paths = []
    for release in releases:
        cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY id",
            (release["release_id"],),
        )
        tracks = cursor.fetchall()
        if not tracks:
            continue

        img = render_release(release, tracks, font_title, font_text, font_bpm, font_meta)

        name = file_name(release)
        img.save(OUTPUT_DIR / name)
        generated_paths.append(OUTPUT_DIR / name)
        generated += 1
        print(f"Generated: {name}")

    conn.close()
    print(f"\nDone. {generated} labels generated in {OUTPUT_DIR}/")

    if open_preview and generated_paths:
        # Open the images in Preview (Mac) to check them before
        # wasting a label.
        subprocess.run(["open", *map(str, generated_paths)])

    print("Next step: python print_labels.py --test  (to see what would print)")
    print("        or: python print_labels.py         (to print)")


if __name__ == "__main__":
    main()
