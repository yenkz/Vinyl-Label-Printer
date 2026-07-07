# Vinyl Label Printer

Generates and prints labels with the album cover, record label, release date,
and track list of your vinyl records (position A1/A2/B1..., title, duration,
BPM and key in Camelot notation) from your Discogs collection, to stick on each record sleeve.

Data sources, in order of priority:

1. **Discogs** — the master source: the record, label, catalog number,
   release date, cover art (from the actual vinyl edition), and track list.
2. **Beatport** — BPM and key (tonality), the standard for electronic music.
   Consulted **for all tracks**, without exception: it's the BPM reference.
   No account or API key required.
3. **Own measurement (YouTube)** — BPM fallback: what Beatport doesn't have
   is searched on Bandcamp/YouTube/SoundCloud, the audio is downloaded and
   measured locally with two detectors.
4. **Deezer** — last resort for BPM (optional, fast).
5. **Bandcamp** — backup for missing data (cover, durations), ideal for
   underground music and small labels. No account needed.
6. **Spotify** — final fallback (cover, durations, ISRC). Optional, requires
   free credentials.

Each track's BPM **records which source it came from**, and the editor
(step 7) displays all sources side by side. Nothing is taken at face value:
**validation is always manual** — you put the ✓ yourself, track by track, in the editor.

Designed to print on a **Brother QL** with a continuous 62mm roll (DK-22205).

## Which printer to buy

- **Brother QL-800** (recommended): fast, affordable, and fully supported
  by this software. The safe choice.
- **Brother QL-600 / QL-600B**: also works with this project (slower and
  slightly cheaper). If you choose it, set `PRINTER_MODEL = "QL-600"`
  in `config.py`.

Both use the same DK rolls. For this project, the **continuous white 62mm roll
(DK-22205)** is enough: each label comes out exactly as long as needed based on the number of tracks.

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
   - (Optional) your getsongbpm.com API key (`GETSONGBPM_API_KEY`) —
     BPM is searched first on Deezer, which is free and needs no key, so this
     is rarely needed.
   - (Optional) credentials for a Spotify app
     (`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, free at
     https://developer.spotify.com/dashboard) — used for step 4, the final
     fallback: cover, durations, and ISRC that no other source has. Note:
     Spotify **no longer** provides BPM to new apps (blocked since Nov 2024);
     that's what Beatport, Deezer, and audio analysis are for. Beatport and
     Bandcamp don't need credentials: those steps work unconfigured.

   The `.env` file is not uploaded to git, so your tokens stay only on your
   computer. Technical settings (printer model, fonts, etc.) remain in `config.py`:
   edit `PRINTER_MODEL` there if your printer isn't the QL-800.

## Usage (each time you want to generate labels)

Run these scripts **in order**, each does one step:

```
python fetch_discogs.py    # 1. Fetch your collection and covers from Discogs
python enrich_beatport.py  # 2. BPM and key from Beatport,
                           #    for ALL tracks (the reference)
python enrich_bandcamp.py  # 3. Missing covers/durations (Bandcamp)
python enrich_spotify.py   # 4. Final fallback: cover, durations, and
                           #    ISRC from Spotify (optional)
python analyze_bpm.py      # 5. The BPM fallback: measures BPM for what
                           #    Beatport didn't have, downloading audio from
                           #    YouTube (slow but effective!)
python enrich_bpm.py       # 6. Last resort for BPM (Deezer, optional)
python edit_bpm.py         # 7. The editor: load/correct by hand and
                           #    VALIDATE each BPM by viewing its sources
python render_labels.py    # 8. Generate the label images
python print_labels.py     # 9. Print pending labels on the Brother QL
```

Notes:

- Step 1 also downloads the cover of each record directly from Discogs
  (the photo of the actual vinyl edition, printed at the top of the label,
  halftoned to black and white). Covers are saved in `covers/`, one per record,
  and don't overwrite each other: to redo one, delete that file from `covers/` and
  run the step again. Very dark or photographic covers lose a lot in halftoning:
  thermal printing has no grays.
- Step 2 searches **every track in your collection** on Beatport — whether it has
  BPM or not: it's the reference source and is consulted without fail — and fetches
  the official BPM and key, which displays on the label in Camelot notation ("8A")
  for harmonic mixing. No account needed: it uses the same anonymous access as
  Beatport's embedded player. It only saves data if the candidate matches artist,
  title, and duration with what Discogs says. Each response is noted as the track's
  source (visible in the editor). If a track already had a measured BPM and Beatport
  says the same, the doubt is resolved (but you put the ✓ in step 7); if they differ,
  it's marked as questionable with the other value one click away. Anything you
  entered manually is never overwritten.
- Step 3 (Bandcamp) fills in what Discogs is missing — cover and durations —
  by searching the album with Bandcamp search API. For underground vinyls and
  small labels, it's often the only source that has them. Bandcamp doesn't publish
  BPM or key.
- Step 4 (Spotify, optional) is the final fallback: cover, durations, and ISRC
  still missing. If the record isn't on Spotify — normal for niche vinyls — no
  problem, almost everything came from earlier steps.
- Step 5 is the **Beatport fallback**: searches each track without BPM on
  Bandcamp, YouTube, or SoundCloud (in that order), verifying the duration matches
  Discogs', downloads the audio to a temp folder, measures BPM locally, and deletes
  it. Takes ~30s per track and can be stopped with Ctrl+C and resumed later. If
  YouTube enters anti-bot mode ("Sign in to confirm you're not a bot"), the script
  continues on SoundCloud only; YouTube unblocks itself in a few hours, or immediately
  if you set `YOUTUBE_COOKIES_NAVEGADOR` in `config.py`. Avoid running two analyses
  at once, that's what triggers the anti-bot. To test with fewer tracks first:
  `python analyze_bpm.py 5`. Tempo is measured with **two detectors** (deeprhythm,
  a neural net very accurate for electronic music, and librosa): if they agree,
  the number is reliable; if not — the classic error of measuring 89 when the real
  tempo is 134 — the track is marked as *questionable* and in step 7 you resolve
  it with a click, with the other candidate right there as a button.
- Step 6 (Deezer, optional) is the last resort: searches for BPM where neither
  Beatport nor measurement could find it. Works for well-known music; for small
  label vinyls Deezer often doesn't have the BPM analyzed.
- If you already had BPM measured with the old analysis version (single detector),
  run **once** `python audit_bpm.py`: re-measures all old automatic BPMs, corrects
  ones that were measured wrong, and notes the re-measurement as source — everything
  is ready for step 7 validation.
- Step 7 (`make edit`) launches a local page (only you see it) with your whole
  collection: search bar, BPM and key fields per track, and each change auto-saves.
  You can write the key in Camelot ("8A") or musical notation ("Am", "f# minor").
  Anything you enter there is saved as `manual` and nothing overwrites it. If you
  prefer a spreadsheet, the old CSV flow still works: `python bpm_manual.py export` / `import`.
- Step 7 is where **validation** happens: each track shows, as pills, all the BPM
  sources ("beatport 128" · "youtube 127.9" · "deezer 128") with details of where
  each number came from. The green ✓ for "validated" **never sets itself**, not
  even if all sources agree: you set it, with the ✓ button (current value is good)
  or by clicking a source pill (that value becomes the track's BPM and is validated
  because you chose it seeing all options). Typing a BPM manually also counts as
  validating it. Questionable ones (sources disagreeing) are highlighted with the
  alternate value one click away. The goal is to see "collection complete: N/N BPM
  validated ✓" at the top — then you're ready to print without worry. On labels,
  an unconfirmed questionable BPM shows with an asterisk (e.g., "129*").
- Step 1 can be repeated when you buy new records: it updates and adds without
  duplicating, **without losing the BPM you already entered**, and removes from
  the database records no longer in your collection. Discogs rate-limits, so it
  takes ~1 second per record.
- Step 8 can generate just some records and show them before printing: `python render_labels.py aphex --view`
  generates labels for records containing "aphex" and opens them in Preview to check.
- Step 9 has a **test mode** that doesn't need a printer: `python print_labels.py --test`
  shows you what labels would print and how many centimeters of roll they'd use,
  without printing or wasting anything.
- Step 9 only prints new labels: already-printed ones move to `labels_output/printed/`.
  To reprint one, move it back to `labels_output/`. You can also print just some:
  `python print_labels.py aphex` prints those containing "aphex" in the filename.

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

**One-time setup:**

1. **Free Soulseek account** — register a username/password in the Soulseek
   client or at https://www.slsknet.org/ (slskd logs in with it).
2. **Install slskd** — download the self-contained binary for macOS (Apple
   Silicon: `osx-arm64`) from https://github.com/slskd/slskd/releases, or run it
   with Docker. Put it somewhere on your `PATH` (e.g. `~/.local/bin/slskd`).
3. **Configure slskd** in its own `slskd.yml` (run `slskd` once to see where it
   lives, or use env vars):
   - your Soulseek `username` / `password`;
   - a **shared folder** — Soulseek expects you to share something; peers often
     block users who share nothing, so point it at a folder with some music;
   - set slskd's **downloads** directory to the same path as `SLSKD_DOWNLOADS_DIR`
     in your `.env` (default `~/Music/Vinyl/_incoming`) so this project can move
     finished files into your library;
   - generate a **web API key** (under `web.authentication.api_keys`).
4. **Fill in your `.env`:** `SLSKD_API_KEY` (the key from step 3), and if you
   changed anything, `SLSKD_HOST`, `SLSKD_DOWNLOADS_DIR`, `MUSIC_DIR`.

**Each time:**

```
make slskd       # in one terminal — starts the daemon, leave it running
make download    # in another terminal — downloads everything still missing
```

Or the scripts directly: `python download_music.py`. Handy variants:

- `make download d=aphex` — only records matching "aphex" (good for a first test).
- `make download force=1` — re-download even records already present.

For each record it first looks for the **whole album from one person** (one
folder = consistent quality and source), matches that folder's files to your
track list by title, and grabs the best-format copy of each; anything missing is
then searched **track by track**. You can stop with **Ctrl+C** anytime — each
finished track is saved immediately and re-runs resume where you left off.
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
analyze_bpm.py      -> Step 5 (fallback: measures BPM from audio)
enrich_bpm.py       -> Step 6 (last resort: Deezer — optional)
audit_bpm.py        -> re-checks old measurements from old version
edit_bpm.py         -> Step 7 (editor and validator for BPM and key)
bpm_manual.py       -> Step 7 alternative (CSV export/import)
render_labels.py    -> Step 8
print_labels.py     -> Step 9
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

- **"Editor Lite" mode**: if your printer has that button turned on (light
  on), you need to turn it off by holding the button for a few seconds —
  it blocks USB printing.

- **Many tracks without BPM**: it's normal, especially for niche editions or
  old vinyls. Use `bpm_manual.py export` / `import` to fill them in yourself
  with Shazam, Tunebat, or your ear. Also watch out for automatic BPM in
  electronic music: sometimes they're double or half the real tempo (70
  instead of 140).
