# Vinyl Label Printer

Generates and prints labels with the album cover, record label, release date,
and track list of your vinyl records (position A1/A2/B1..., title, duration,
BPM and key in Camelot notation) from your Discogs collection, to stick on each record sleeve.

Data sources, in order of priority:

1. **Discogs** — the master source: the record, label, catalog number,
   release date, cover art (from the actual vinyl edition), and track list.
2. **Beatport** — BPM and key (tonality), the standard for electronic music.
   Consulted for every newly imported track: it's the BPM reference.
   No account or API key required.
3. **Own measurement (audio fallback)** — what Beatport doesn't have is
   searched on Bandcamp/YouTube/SoundCloud, downloaded temporarily, and
   measured locally. BPM and key each use two independent detectors.
4. **Bandcamp** — backup for missing data (cover, durations), ideal for
   underground music and small labels. No account needed.
5. **Spotify** — final fallback (cover, durations, ISRC). Optional, requires
   free credentials.

Each track's BPM **records which source it came from**, and the editor
(step 6) displays all sources side by side. A track found independently in both
Discogs and Beatport is **confirmed automatically in the database**; unmatched
fallback measurements remain for manual review in the editor.

Designed to print on a **Brother QL** with a continuous 62mm black/red-on-white
roll (DK-2251).

## Which printer to buy

- **Brother QL-800** (recommended): fast, affordable, and fully supported
  by this software. The safe choice.
- **Brother QL-600 / QL-600B**: also works with this project (slower and
  slightly cheaper). If you choose it, set `PRINTER_MODEL = "QL-600"`
  in `config.py`.

Both use the same DK rolls. For this project, the **continuous white 62mm roll
(DK-2251)** is enough: each label comes out exactly as long as needed based on
the number of tracks.

> Note: these are direct thermal printers with no ink. Prints last for years but fade
> with heat, sun, and direct contact with soft PVC sleeves. Paper/polyethylene
> sleeves have no problems.

## Installation (one time only)

1. Install Python 3 if you don't have it (on Mac: `brew install python`).
2. On Mac, to make USB connection work: `brew install libusb`
3. Open Terminal in this folder and run:
   ```
   make setup
   ```
   (installs dependencies in a project-specific environment and creates the `.env` file)
4. Copy `.env.example` as `.env` and fill in your personal data there
   (the `make setup` above already copies it for you):
   - Your Discogs token (`DISCOGS_USER_TOKEN`)
   - Your Discogs username (`DISCOGS_USERNAME`)
   - (Optional) credentials for a Spotify app
     (`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, free at
     https://developer.spotify.com/dashboard) — used for step 4, the final
     fallback: cover, durations, and ISRC that no other source has. Note:
     Spotify **no longer** provides BPM to new apps (blocked since Nov 2024);
     that's what Beatport and audio analysis are for. Beatport and
     Bandcamp don't need credentials: those steps work unconfigured.

   The `.env` file is not uploaded to git, so your tokens stay only on your
   computer. Technical settings (printer model, fonts, etc.) remain in `config.py`:
   edit `PRINTER_MODEL` there if your printer isn't the QL-800.

## Usage (each time you want to generate labels)

The easiest workflow is through `make`. Every automatic command is
**incremental by default**: after `make fetch` imports records you just added
to Discogs, the following steps operate only on those new records.

```
make fetch       # import new Discogs records
make beatport    # new tracks only
make bandcamp    # new records only
make spotify     # new records only (optional)
make analyze     # missing BPMs or keys on new records only
make render      # create/update labels with 100% validated BPMs
```

`make todo` runs the automatic chain in one go. To deliberately revisit the
whole collection, add `full=1` to any command—for example `make beatport
full=1`, `make render full=1`, or `make todo full=1`. Naming a render filter is
also explicit, so `make render d=aphex` regenerates the matching record even if
it is not new.

The scripts can also be run directly in order:

```
python fetch_discogs.py    # 1. Fetch NEW records and covers from Discogs
python enrich_beatport.py  # 2. BPM and key for new tracks (the reference)
python enrich_bandcamp.py  # 3. Missing covers/durations (Bandcamp)
python enrich_spotify.py   # 4. Final fallback: cover, durations, and
                           #    ISRC from Spotify (optional)
python analyze_bpm.py      # 5. Audio fallback: measures missing BPM/key,
                           #    downloading audio temporarily (slow but effective!)
python edit_bpm.py         # 6. The editor: load/correct by hand and
                           #    VALIDATE each BPM by viewing its sources
python render_labels.py    # 7. Create/update fully validated labels
python print_labels.py     # 8. Print pending labels on the Brother QL
```

Notes:

- Step 1 also downloads the cover of each new record directly from Discogs
  (the photo of the actual vinyl edition, printed at the top of the label,
  halftoned to black and white). Covers are saved in `covers/`, one per record,
  and don't overwrite each other. To refresh existing Discogs data, run
  `make fetch full=1`. Very dark or photographic covers lose a lot in halftoning:
  thermal printing has no grays.
- Step 2 searches **every track on each new record** on Beatport — whether it has
  BPM or not: it's the reference source — and fetches
  the official BPM and key, which displays on the label in Camelot notation ("8A")
  for harmonic mixing. No account needed: it uses the same anonymous access as
  Beatport's embedded player. It only saves data if the candidate matches artist,
  title, and duration with what Discogs says. Each response is noted as the track's
  source (visible in the editor). Because the track was discovered on Discogs and
  independently matched on Beatport, a found Beatport BPM is saved as verified
  automatically. It replaces an unverified fallback measurement, while anything
  you entered or previously confirmed manually is never overwritten. Beatport's
  key is saved alongside the BPM whenever it is available.
- Step 3 (Bandcamp) fills in what Discogs is missing — cover and durations —
  by searching the album with Bandcamp search API. For underground vinyls and
  small labels, it's often the only source that has them. Bandcamp doesn't publish
  BPM or key.
- Step 4 (Spotify, optional) is the final fallback: cover, durations, and ISRC
  still missing. If the record isn't on Spotify — normal for niche vinyls — no
  problem, almost everything came from earlier steps.
- Step 5 is the **audio-analysis fallback**: searches each track without BPM or key on
  Bandcamp, YouTube, or SoundCloud (in that order), verifying the duration matches
  Discogs', downloads the audio to a temp folder, measures it locally, and deletes
  it. Takes roughly 30s or more per track and can be stopped with Ctrl+C and
  resumed later. If
  YouTube enters anti-bot mode ("Sign in to confirm you're not a bot"), the script
  continues on SoundCloud only; YouTube unblocks itself in a few hours, or immediately
  if you set `YOUTUBE_COOKIES_NAVEGADOR` in `config.py`. Avoid running two analyses
  at once, that's what triggers the anti-bot. To control one batch's size and
  delay between tracks, run `make analyze n=20 pace=8` (20 tracks, waiting 8
  seconds between them). The default pace is 3 seconds; use `pace=0` to disable
  the delay. The equivalent direct command is `python analyze_bpm.py 20 --pace 8`.
  Tempo is measured with **two detectors** (deeprhythm,
  a neural net very accurate for electronic music, and librosa): if they agree,
  the number is reliable; if not — the classic error of measuring 89 when the real
  tempo is 134 — the track is marked as *questionable* and in step 6 you resolve
  it with a click, with the other candidate right there as a button.
  Musical key is measured over the **complete track** with Essentia's EDM-specific
  `bgate` profile and a separate librosa harmonic-chroma classifier. Exact agreement
  is accepted; disagreement or a single-detector result is highlighted for review,
  with both candidates and detector scores visible in `make edit`. Beatport and
  manually entered keys are never overwritten by local analysis.
- If you already had BPM measured with the old analysis version (single detector),
  run **once** `python audit_bpm.py`: re-measures all old automatic BPMs, corrects
  ones that were measured wrong, and notes the re-measurement as source — everything
  is ready for step 6 validation.
- Step 6 (`make edit`) launches a local page (only you see it) with your whole
  collection: search bar, BPM and key fields per track, and each change auto-saves.
  You can write the key in Camelot ("8A") or musical notation ("Am", "f# minor").
  Anything you enter there is saved as `manual` and nothing overwrites it. If you
  prefer a spreadsheet, the old CSV flow still works: `python bpm_manual.py export` / `import`.
- Step 6 is where **remaining manual validation** happens: each track shows, as
  pills, all the BPM sources (for example, "beatport 128" · "youtube 127.9")
  with details of where
  each number came from. Successful Discogs + Beatport matches already show the
  green ✓. For fallback measurements, set it with the ✓ button (current value
  is good) or by clicking a source pill (that value becomes the track's BPM and is
  validated because you chose it seeing all options). Typing a BPM manually also
  counts as validating it. Questionable fallback measurements are highlighted with
  the alternate value one click away. Key candidates appear under the key field in
  the same editor. The goal is to validate every BPM and resolve doubtful keys;
  printing itself remains gated by BPM validation, so a genuinely atonal track may
  still have an empty key.
- Step 1 can be repeated when you buy new records: it skips records already
  saved, adds only the delta without duplicating, and removes from the database
  records no longer in your collection. `make fetch full=1` refreshes all saved
  Discogs metadata while preserving BPM, key, ISRC, and downloaded-audio data.
  Discogs rate-limits detailed imports, so those take ~1 second per new record.
- Step 7 can generate just some records and show them before printing: `python render_labels.py aphex --view`
  generates labels for records containing "aphex" and opens them in Preview to check.
  A record is rendered only when every track has a BPM and its green ✓ in
  `make edit`; incomplete records stay pending for the next `make render` run.
  Existing images are compared pixel-for-pixel with the current database data:
  unchanged labels are skipped, while changed labels are replaced. If the old
  label is already under `labels_output/printed/`, its changed replacement is
  created in `labels_output/` so it becomes pending for reprinting.
- Step 8 has a **test mode** that doesn't need a printer: `python print_labels.py --test`
  shows you what labels would print and how many centimeters of roll they'd use,
  without printing or wasting anything.
- Printing follows the layout from `Fantastic Man - The Axis of People.lbx`:
  62mm continuous media, 270° artwork rotation, fit to the printable width,
  error-diffusion monochrome conversion, and an automatic cut after each label.
- Step 8 only prints new labels. After every USB job, it asks you to confirm
  that the physical label printed and cut completely; only a confirmed label
  moves to `labels_output/printed/`. To reprint one, move it back to
  `labels_output/`. You can also print just some:
  `python print_labels.py aphex` prints those containing "aphex" in the filename.
  `make print` refreshes changed renders first, refuses labels that are not 100%
  BPM-validated, and requires physical confirmation after every print.
  To print every pending label continuously without per-label pauses, run
  `make print batch=1`. It still cuts after each label and stops on a reported
  printer fault. At the end, enter how many completed in order; only those are
  moved into `labels_output/printed/`. Two-color DK-2251 printing is limited to
  about 24mm/s, so a large batch can take several minutes.

## Download digital copies (Soulseek) — optional

Once your collection is in the database, you can download a **digital copy of
each record** to play out — sourced from **Soulseek** (a peer-to-peer music
network), preferring lossless. Files land, tagged and with cover art, in a tidy
per-record library:

```
~/Music/Vinyl/<Artist> - <Album> (<CATNO>)/<position> <Title>.<ext>
```

Format preference is **AIFF → FLAC → WAV → MP3 320**, kept as found (no
conversion; rekordbox/Serato read all four). This is a personal copy of records
you already own on vinyl.

The downloading itself is done by **slskd**, a small Soulseek program you run in
the background; this project just drives it.

**One-time setup (Docker — recommended):**

slskd's config file, `slskd.yml`, lives **inside the container**, so you must
mount a folder from your Mac over it — otherwise the file is invisible and
lost when the container is removed. We keep it at `~/slskd/slskd.yml`.

1. **Create the folders and config** — `mkdir -p ~/slskd ~/Music/Vinyl/_incoming`,
   then put a `slskd.yml` in `~/slskd/` with: your Soulseek `username`/`password`
   (any you like — Soulseek registers it automatically on first login, no signup
   needed), `directories.downloads: /app/downloads`, `shares.directories: [/music]`,
   and an API key under `web.authentication.api_keys`.
2. **Run the container** (the `-v` flags map container paths to your Mac):

   ```
   docker run -d --name slskd --restart unless-stopped \
     -p 5030:5030 -p 50300:50300 \
     -v ~/slskd:/app \
     -v ~/Music/Vinyl/_incoming:/app/downloads \
     -v ~/Music/Vinyl:/music:ro \
     slskd/slskd:latest
   ```

   `--restart unless-stopped` means Docker Desktop starts it for you from now
   on — no daemon to babysit. Web UI: http://localhost:5030 (user/pass from
   `slskd.yml`, default `slskd`/`slskd`).
3. **Fill in your `.env`:** `SLSKD_API_KEY` = the key from `slskd.yml`.
4. After any edit to `~/slskd/slskd.yml`: `docker restart slskd`.

(Alternative without Docker: download the `osx-arm64` binary from
https://github.com/slskd/slskd/releases onto your `PATH`; run `slskd` once and
it prints where it created `slskd.yml` — usually `~/.local/share/slskd/`.
Configure the same things, using real Mac paths instead of `/app/...`.)

**Each time:**

```
make download    # downloads everything still missing
```

(With Docker, slskd is already running in the background. `make slskd` checks
it's up / starts it. Binary users: run `make slskd` in its own terminal first.)

Or the scripts directly: `python download_music.py`. Handy variants:

- `make download d=aphex` — only records matching "aphex" (good for a first test).
- `make download force=1` — re-download even records already present.
- `make download j=12` — search 12 records at a time (default is 8).

For each record it first looks for the **whole album from one person** (one
folder = consistent quality and source), matches that folder's files to your
track list by title, and queues the best-format copy of each; anything missing
is then searched **track by track**. Searching runs several records **in
parallel**, and the downloads themselves are slskd's job: the script checks in
on them every few seconds and files each finished track into the library.
Sitting in someone's upload queue is normal on Soulseek — sometimes for a long
while — so queued or half-done transfers are **never cancelled**; only when a
source actually fails (rejects, errors out, disappears) is the track retried
from the next person sharing it. You can stop with **Ctrl+C** anytime —
finished tracks are already saved, whatever is still queued keeps downloading
inside slskd, and the next run collects it before searching for anything else.
Records already fully downloaded are skipped without touching the network.

At the end it lists any tracks it couldn't find, so you can grab those by hand
on Soulseek or buy them (Bandcamp is ideal for small-label electronic). Dropping
a purchased lossless file into the record's folder is a fine way to upgrade a
rip later.

## Project structure

```
.env                -> your personal data (tokens) — not uploaded to git
config.py           -> technical settings (printer, labels, fonts)
db.py               -> manages local database (SQLite)
common.py           -> shared helpers (matching, covers, keys)
fetch_discogs.py    -> Step 1 (collection + covers, master source)
enrich_beatport.py  -> Step 2 (BPM and key from Beatport, always)
enrich_bandcamp.py  -> Step 3 (missing covers/durations)
enrich_spotify.py   -> Step 4 (final fallback — optional)
analyze_bpm.py      -> Step 5 (fallback: measures BPM and key from audio)
audit_bpm.py        -> re-checks old measurements from old version
edit_bpm.py         -> Step 6 (editor and validator for BPM and key)
bpm_manual.py       -> Step 6 alternative (CSV export/import)
render_labels.py    -> Step 7
print_labels.py     -> Step 8
download_music.py   -> optional: download digital copies from Soulseek (slskd)
vinyl_labels.db     -> auto-created, stores your entire collection
covers/             -> downloaded covers (one per record)
labels_output/      -> generated images pending printing
labels_output/printed/ -> already printed labels
```

## Common issues

- **"Font not found"**: open `config.py` and change `FONT_PATH` to the path
  of a .ttf font you actually have installed. The script still works, just
  looks less polished.

- **Printer won't print / not detected**: check it's plugged in and turned on,
  and on Mac that you've installed libusb. If it still doesn't show up, run
  `brother_ql discover`, copy the ID it shows and paste it in `config.py`,
  under `PRINTER_IDENTIFIER`.

- **Status light starts blinking red when a job is sent**: the QL-800 is
  rejecting the job because it sees no roll, the wrong roll or print-color
  mode, an open cover, or a feed/cutter/communication error. This project uses
  the 62mm continuous DK-2251 black/red-on-white roll (not monochrome DK-22205
  or 62 x 100mm die-cut labels). `make print` sends the required two-color
  raster mode even though the artwork itself is black-only, checks the model
  and installed media before sending, and prints reported hardware errors.
  Reseat the roll and its leading edge, close the cover firmly, and power-cycle
  the printer if the red light does not clear.

- **"Editor Lite" mode**: if your printer has that button turned on (light
  on), you need to turn it off by holding the button for a few seconds —
  it blocks USB printing.

- **Many tracks without BPM**: it's normal, especially for niche editions or
  old vinyls. Use `bpm_manual.py export` / `import` to fill them in yourself
  with Shazam, Tunebat, or your ear. Also watch out for automatic BPM in
  electronic music: sometimes they're double or half the real tempo (70
  instead of 140).
