"""
print_labels.py — STEP 8

Takes the images in labels_output/ and sends them to print, one by one,
to your Brother QL printer connected via USB.

The print setup follows the Brother P-touch template
"Fantastic Man - The Axis of People.lbx": 62mm black/red continuous media,
artwork rotated 270 degrees, fitted to the 696-dot printable width,
error-diffusion monochrome artwork, and automatic cutting.

After each USB job, you must confirm that the physical label printed and cut
completely. Only then is it moved to labels_output/printed/, so next time only
confirmed labels are skipped. If you want to reprint one, move it back to
labels_output/ and run this again (or force a fresh canonical copy with
python -m vinyl_labels render FILTER --all).

Batch mode sends every pending label without pausing between confirmations.
After the run, enter how many labels completed in order; only those are marked
as printed.

How to run it:
    python -m vinyl_labels print            # print all pending
    python -m vinyl_labels print aphex      # filter pending labels
    python -m vinyl_labels print --test     # test mode: shows what would print and
                                      # how long, without printer and without
                                      # wasting a label (can combine with filter)
    python -m vinyl_labels print --batch    # print continuously; confirm once

Before running it for the first time:
  - Connect the printer via USB, powered on with the 62mm continuous
    roll loaded.
  - If your printer has "Editor Lite" mode (QL-600B, QL-700...),
    disable it (hold the button until the light goes off),
    because it blocks USB printing.
"""

import argparse
import time
from pathlib import Path

from brother_ql import BrotherQLRaster
from brother_ql.backends.helpers import discover, get_printer, get_status, send
from brother_ql.conversion import convert
from PIL import Image

from vinyl_labels import config
from vinyl_labels.db import get_connection, init_db
from vinyl_labels.paths import project_path

from .render_labels import file_name, release_id_from_path, unique_artifact_path

OUTPUT_DIR = project_path(config.OUTPUT_DIR)
PRINTED_DIR = OUTPUT_DIR / "printed"

# Settings copied from "Fantastic Man - The Axis of People.lbx". For the 62mm
# endless roll, brother_ql scales the rotated image to 696 printable dots and
# supplies its standard 35-dot feed margin (about 3mm).
PRINT_ROTATION = 270
PRINTABLE_WIDTH_PX = 696
FEED_MARGIN_PX = 35
TWO_COLOR_PRINT_SPEED_MM_S = 24
CUT_SETTLE_SECONDS = 1.0
EXPECTED_MEDIA_WIDTH_MM = 62
EXPECTED_MEDIA_TYPE = "Continuous length tape"
EXPECTED_MEDIA_CATEGORY = "DK"
PRINT_LABEL = "62red"
PRINT_ROLL = "DK-2251 black/red on white"

def estimated_length_mm(img):
    """Physical roll length after the template's rotation and width fitting."""
    rotated_width, rotated_height = img.height, img.width
    scaled_height = round(rotated_height * PRINTABLE_WIDTH_PX / rotated_width)
    return round((scaled_height + FEED_MARGIN_PX) / 11.81)


def print_wait_seconds(length_mm):
    """Conservative time for DK-2251 two-color printing plus its cut."""
    return length_mm / TWO_COLOR_PRINT_SPEED_MM_S + CUT_SETTLE_SECONDS


def prepare_for_print(img):
    """Applies the LBX rotation, width fit, and error diffusion.

    DK-2251 must be sent as a two-color job even though this artwork only uses
    black. Dither explicitly first because brother_ql's two-color conversion
    does not apply its ``dither`` option.
    """
    prepared = img.rotate(PRINT_ROTATION, expand=True)
    if prepared.width != PRINTABLE_WIDTH_PX:
        height = int(PRINTABLE_WIDTH_PX / prepared.width * prepared.height)
        prepared = prepared.resize(
            (PRINTABLE_WIDTH_PX, height),
            Image.Resampling.LANCZOS,
        )
    return (
        prepared.convert("L")
        .convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        .convert("RGB")
    )


def validated_images(paths):
    """Splits paths into (printable, blocked), rejecting stale/duplicate IDs."""
    init_db()
    conn = get_connection()
    printable = []
    blocked = []
    by_release = {}
    for path in paths:
        release_id = release_id_from_path(path)
        if release_id is None:
            blocked.append(path)
            continue
        by_release.setdefault(release_id, []).append(path)

    for release_id, release_paths in by_release.items():
        # Never guess which copy to print. A normal render archives superseded
        # names; if copies remain, stopping both is safer than printing twice.
        if len(release_paths) != 1:
            blocked.extend(release_paths)
            continue

        path = release_paths[0]
        release = conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()
        # A corrected artist/title creates a new canonical filename. Block the
        # older pending artifact even if its tracks are still fully validated.
        if release is None or path.name != file_name(release):
            blocked.append(path)
            continue
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN bpm IS NOT NULL AND bpm_verified = 1 THEN 1 ELSE 0 END) AS valid"
            " FROM tracks WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if row["total"] and row["valid"] == row["total"]:
            printable.append(path)
        else:
            blocked.append(path)
    conn.close()
    return printable, blocked


def move_to_printed(path, printed_dir=PRINTED_DIR):
    """Moves a confirmed label without overwriting earlier print history."""
    printed_dir = Path(printed_dir)
    printed_dir.mkdir(exist_ok=True)
    destination = unique_artifact_path(printed_dir, Path(path).name)
    Path(path).rename(destination)
    return destination


def find_printer():
    """Returns the printer identifier: the configured one if defined,
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
            "and paste the identifier in vinyl_labels/config.py (PRINTER_IDENTIFIER)."
        )
        return None

    identifier = devices[0]["identifier"]
    print(f"Printer detected: {identifier}\n")
    return identifier


def read_printer_status(printer_identifier):
    """Asks the printer which model, roll, and error state it sees."""
    printer = get_printer(
        printer_identifier=printer_identifier,
        backend_identifier=config.PRINTER_BACKEND,
    )
    try:
        return get_status(printer)
    finally:
        printer.dispose()


def media_description(status):
    """Human-readable media description from a Brother status packet."""
    width = status.get("media_width", 0)
    length = status.get("media_length", 0)
    media_type = status.get("media_type", "unknown media")
    size = f"{width}mm"
    if length:
        size += f" x {length}mm"
    return f"{size} {media_type.lower()}"


def printer_preflight_problems(status):
    """Returns reasons why sending a 62mm continuous-roll job is unsafe."""
    problems = []
    errors = status.get("errors") or []
    if errors:
        problems.append("Printer error: " + "; ".join(errors))
    elif status.get("status_type") == "Error occurred":
        problems.append("Printer reports an unspecified error from the previous job.")

    model = status.get("model")
    if model not in (config.PRINTER_MODEL, "Unknown"):
        problems.append(
            f"Configured for {config.PRINTER_MODEL}, but the connected printer "
            f"reports itself as {model}."
        )

    if (
        status.get("media_category") != EXPECTED_MEDIA_CATEGORY
        or status.get("media_type") != EXPECTED_MEDIA_TYPE
        or status.get("media_width") != EXPECTED_MEDIA_WIDTH_MM
        or status.get("media_length") != 0
    ):
        problems.append(
            f"Wrong roll: this job requires a 62mm continuous DK roll "
            f"({PRINT_ROLL}), but the printer reports {media_description(status)}."
        )
    return problems


def show_preflight(printer_identifier):
    """Checks the printer and loaded roll, returning True only when ready."""
    try:
        status = read_printer_status(printer_identifier)
    except Exception as e:
        print(
            "Couldn't read the printer status, so no job was sent. "
            f"Power-cycle/reconnect it and retry. ({e})"
        )
        return False

    problems = printer_preflight_problems(status)
    if problems:
        print("Printer is not ready; no job was sent:")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "Open the roll compartment, seat the roll and its leading edge, "
            "then close the cover firmly and retry."
        )
        return False

    print(
        f"Printer ready: {status.get('model', config.PRINTER_MODEL)}, "
        f"{media_description(status)}; using {PRINT_ROLL} mode.\n"
    )
    return True


def confirm_batch(sent_paths):
    """Marks the successfully completed prefix of a continuous batch."""
    if not sent_paths:
        return 0

    total = len(sent_paths)
    while True:
        response = input(
            f"\nHow many of the {total} sent labels printed and cut completely "
            f"in order? (0-{total}, or 'all'): "
        ).strip().lower()
        if response == "all":
            completed = total
            break
        try:
            completed = int(response)
        except ValueError:
            completed = -1
        if 0 <= completed <= total:
            break
        print(f"Please enter a number from 0 to {total}, or 'all'.")

    for path in sent_paths[:completed]:
        move_to_printed(path)

    if completed:
        print(f"Marked the first {completed} label(s) as printed.")
    if completed < total:
        print(f"Kept {total - completed} sent but unconfirmed label(s) pending.")
    return completed


def parse_arguments(arguments=None):
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels print",
        description="Print pending validated labels.",
    )
    parser.add_argument("filter", nargs="?", help="filename text to match")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="convert and estimate without printing")
    mode.add_argument("--batch", action="store_true", help="send continuously, confirm once")
    return parser.parse_args(arguments)


def main(arguments=None):
    args = parse_arguments(arguments)
    test_mode = args.test
    batch_mode = args.batch
    filter_str = (args.filter or "").lower()

    pending = sorted(p for p in OUTPUT_DIR.glob("*.png") if filter_str in p.name.lower())
    images, blocked = validated_images(pending)

    if blocked:
        print(
            f"Skipping {len(blocked)} unsafe pending label(s): each label must "
            "have a current release filename, a unique release ID, and 100% "
            "BPM validation. Run `make render` after any metadata change."
        )

    if not images:
        if blocked:
            print("No current, uniquely identified, fully validated labels are ready to print.")
        elif filter_str:
            print(f"No pending labels containing '{filter_str}'.")
        else:
            print(f"No pending labels in {OUTPUT_DIR}/.")
        print("Already printed ones are in labels_output/printed/. To reprint,")
        print("run python -m vinyl_labels render FILTER --all (or move it back);")
        print("to generate new ones normally run: python -m vinyl_labels render")
        return 0

    if test_mode:
        print(f"TEST MODE: {len(images)} pending labels. Nothing prints.\n")
    else:
        print(f"Found {len(images)} labels to print.\n")

        printer_identifier = find_printer()
        if printer_identifier is None:
            return 1
        if not show_preflight(printer_identifier):
            return 1

        if batch_mode:
            prompt = (
                f"Print all {len(images)} labels continuously now? "
                "There will be no pauses. (y/n): "
            )
        else:
            prompt = f"Print the {len(images)} labels now? (y/n): "
        response = input(prompt).strip().lower()
        if response != "y":
            print("Cancelled. Nothing printed.")
            return 0

        PRINTED_DIR.mkdir(exist_ok=True)

    printed = 0
    operational_failed = False
    sent_paths = []
    total_length_mm = 0
    for i, path in enumerate(images, start=1):
        try:
            with Image.open(path) as img:
                img.load()
                # Estimate after applying the same 270-degree rotation, width
                # fit, and feed margin used for the real print below.
                length_mm = estimated_length_mm(img)
                print_img = prepare_for_print(img)
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
                [print_img],
                label=PRINT_LABEL,
                rotate=0,  # already rotated exactly like the LBX template
                red=True,  # required for DK-2251, even for black-only artwork
                hq=False,  # the print-quality flag is invalid in two-color mode
                cut=True,
            )

            if test_mode:
                # Up to here we validate that the label converts correctly
                # to the printer format; we just need to send it.
                continue

            # QL-800 + pyusb on macOS often does not return the optional status
            # packets expected by brother_ql's blocking mode. A successful USB
            # write only means the job reached the device; it does not prove
            # that the physical label printed completely. Keep the PNG pending
            # until the user has inspected and explicitly confirmed it below.
            status = send(
                instructions=instructions,
                printer_identifier=printer_identifier,
                backend_identifier=config.PRINTER_BACKEND,
                blocking=False,
            )
            if status.get("outcome") != "sent":
                raise RuntimeError(f"printer did not accept the job: {status}")
            if batch_mode:
                sent_paths.append(path)

            # DK-2251 two-color mode is much slower than monochrome (24mm/s).
            # Waiting for the physical length plus the cut prevents a batch
            # from writing the next job while this one is still printing.
            time.sleep(print_wait_seconds(length_mm))

            # A non-blocking USB write only proves that the bytes left the Mac.
            # Ask the printer whether it rejected them (wrong roll, feed/cutter
            # problem, open cover, etc.) before asking the user to confirm.
            try:
                post_send_status = read_printer_status(printer_identifier)
            except Exception:
                # Some macOS/libusb combinations do not return a status packet
                # reliably after a job. Physical confirmation remains the safe
                # fallback in that case.
                post_send_status = None
            if post_send_status:
                problems = printer_preflight_problems(post_send_status)
                if problems:
                    operational_failed = True
                    print("   -> The printer rejected the job:")
                    for problem in problems:
                        print(f"      - {problem}")
                    print(
                        "      Fix the roll/feed/cover, power-cycle the printer "
                        "if the red light remains, then run make print again."
                    )
                    break

            if batch_mode:
                print("   Sent; continuing with the batch.")
                continue

            while True:
                confirmation = input(
                    "   Did it print and cut completely? "
                    "(y = confirm / n = keep pending / q = stop): "
                ).strip().lower()
                if confirmation in {"y", "n", "q"}:
                    break
                print("   Please enter y, n, or q.")
        except KeyboardInterrupt:
            print(
                "\nStopped. The current label stayed pending because its "
                "physical print was not confirmed."
            )
            break
        except EOFError:
            print(
                "\nNo confirmation received. The current label stayed pending; "
                "stopping safely."
            )
            break
        except Exception as e:
            operational_failed = True
            print(f"   -> Error with this label: {e}")
            if not test_mode:
                print("   Stopping before another job is sent to the printer.")
                break
            continue

        if batch_mode:
            continue
        elif confirmation == "y":
            move_to_printed(path)
            printed += 1
            print("   Confirmed; moved to labels_output/printed/.")
        elif confirmation == "n":
            print("   Not confirmed; kept in labels_output/ for retry.")
        else:
            print("   Not confirmed; kept pending. Stopped before the next label.")
            break

    if test_mode:
        print(f"\nIn total about {total_length_mm / 10:.0f}cm of roll would be used.")
        print("To actually print: python -m vinyl_labels print")
    else:
        if batch_mode:
            try:
                printed = confirm_batch(sent_paths)
            except (EOFError, KeyboardInterrupt):
                print(
                    "\nNo batch confirmation received. All sent labels stayed "
                    "pending for safe retry."
                )
        print(f"\nDone. {printed} of {len(images)} labels confirmed printed.")
        if printed < len(images):
            print("Failed or unconfirmed labels stayed in labels_output/ to retry.")
    return int(operational_failed)


if __name__ == "__main__":
    raise SystemExit(main())
