"""
print_labels.py — STEP 5

Takes the images in labels_output/ and sends them to print, one by one,
to your Brother QL printer connected via USB.

Each label that prints successfully is moved to labels_output/printed/,
so next time only new ones are printed. If you want to reprint one, move it
back to labels_output/ and run this again.

How to run it:
    python print_labels.py            # print all pending
    python print_labels.py aphex      # only those containing "aphex"
    python print_labels.py --test     # test mode: shows what would print and
                                      # how long, without printer and without
                                      # wasting a label (can combine with filter)

Before running it for the first time:
  - Connect the printer via USB, powered on with the 62mm continuous
    roll loaded.
  - If your printer has "Editor Lite" mode (QL-600B, QL-700...),
    disable it (hold the button until the light goes off),
    because it blocks USB printing.
"""

import sys
from pathlib import Path

from PIL import Image
from brother_ql import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import discover, send

import config

OUTPUT_DIR = Path(__file__).parent / config.OUTPUT_DIR
PRINTED_DIR = OUTPUT_DIR / "printed"


def find_printer():
    """Returns the printer identifier: the one from config.py if defined,
    or the first Brother device found via USB."""
    if config.PRINTER_IDENTIFIER:
        return config.PRINTER_IDENTIFIER

    try:
        devices = discover(backend_identifier=config.PRINTER_BACKEND)
    except Exception as e:
        print(f"Couldn't search for printers via USB: {e}")
        devices = []

    if not devices:
        print(
            "\nNo printer found connected.\n"
            "Check that it's plugged in and powered on. If it still doesn't appear,\n"
            "run in the terminal:  brother_ql discover\n"
            "and paste the identifier in config.py (PRINTER_IDENTIFIER)."
        )
        return None

    identifier = devices[0]["identifier"]
    print(f"Printer detected: {identifier}\n")
    return identifier


def main():
    args = sys.argv[1:]
    test_mode = "--test" in args
    filter_str = next((a.lower() for a in args if not a.startswith("--")), "")

    images = sorted(p for p in OUTPUT_DIR.glob("*.png") if filter_str in p.name.lower())

    if not images:
        if filter_str:
            print(f"No pending labels containing '{filter_str}'.")
        else:
            print(f"No pending labels in {OUTPUT_DIR}/.")
        print("Already printed ones are in labels_output/printed/ (move them")
        print("back to labels_output/ if you want to reprint them), and to")
        print("generate new ones run: python render_labels.py")
        return

    if test_mode:
        print(f"TEST MODE: {len(images)} pending labels. Nothing prints.\n")
    else:
        print(f"Found {len(images)} labels to print.\n")

        printer_identifier = find_printer()
        if printer_identifier is None:
            return

        response = input(f"Print the {len(images)} labels now? (y/n): ").strip().lower()
        if response != "y":
            print("Cancelled. Nothing printed.")
            return

        PRINTED_DIR.mkdir(exist_ok=True)

    printed = 0
    total_length_mm = 0
    for i, path in enumerate(images, start=1):
        try:
            img = Image.open(path)
            # ~11.8 pixels per mm (300dpi): we use this to estimate how much
            # roll each label consumes.
            length_mm = round(img.height / 11.81)
            total_length_mm += length_mm

            if test_mode:
                print(f"[{i}/{len(images)}] {path.name}  ({length_mm}mm of roll)")
            else:
                print(f"[{i}/{len(images)}] Printing {path.name}...")

            # The raster object accumulates instructions, so we use
            # a new one for each label.
            qlr = BrotherQLRaster(config.PRINTER_MODEL)
            instructions = convert(
                qlr,
                [img],
                label="62",  # 62mm continuous roll
                rotate="auto",
                threshold=70,
                cut=True,
            )

            if test_mode:
                # Up to here we validate that the label converts correctly
                # to the printer format; we just need to send it.
                continue

            send(
                instructions=instructions,
                printer_identifier=printer_identifier,
                backend_identifier=config.PRINTER_BACKEND,
                blocking=True,
            )
        except Exception as e:
            print(f"   -> Error with this label, continuing: {e}")
            continue

        # If we get here, it worked: move it to printed/ so it doesn't
        # get printed again next time.
        path.rename(PRINTED_DIR / path.name)
        printed += 1

    if test_mode:
        print(f"\nIn total about {total_length_mm / 10:.0f}cm of roll would be used.")
        print("To actually print: python print_labels.py")
    else:
        print(f"\nDone. {printed} of {len(images)} labels printed.")
        if printed < len(images):
            print("The ones that failed stayed in labels_output/ to retry.")


if __name__ == "__main__":
    main()
