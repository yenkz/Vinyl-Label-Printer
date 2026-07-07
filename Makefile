# Shortcuts so you don't have to remember the commands.
#
# Usage:
#   make            -> show this help
#   make setup      -> install everything (one time only)
#   make fetch      -> step 1: collection and covers from Discogs
#   make beatport   -> step 2: BPM and key from Beatport, for ALL
#                      tracks (make beatport n=5 to test)
#   make bandcamp   -> step 3: missing covers/durations (Bandcamp)
#   make spotify    -> step 4: final fallback (cover, durations, ISRC)
#   make analyze    -> step 5: measures what Beatport didn't have, from
#                      YouTube (make analyze n=5 to test with 5)
#   make bpm        -> step 6: last resort for BPM (Deezer, optional)
#   make audit      -> re-checks measurements from old version
#                      (one time only; make audit n=5 to test)
#   make edit       -> step 7: BPM and key editor and VALIDATOR (you put
#                      the ✓ there, seeing all sources)
#   make render     -> step 8: generates all labels
#   make test       -> step 9 in test mode (no printer)
#   make print      -> step 9: prints pending labels
#
# Optional, separate from the labels: download a digital copy of the
# collection from Soulseek (needs the slskd daemon, see the README).
#   make slskd      -> start the slskd daemon (keep it open in its own
#                      terminal)
#   make download   -> download everything still missing into ~/Music/Vinyl
#                      (make download d=aphex for one record; add force=1
#                      to re-download)
#
# Steps with filter accept d=text (d for record):
#   make render d=aphex   -> only records containing "aphex"
#   make view d=aphex     -> same + opens them in Preview
#   make test d=aphex
#   make print d=aphex
#
# And to do everything at once (fetch + beatport + bandcamp + spotify +
# bpm + render):
#   make todo

# uv lives in ~/.local/bin, which is not always in make's PATH,
# so we search for it and if not found, use that path directly.
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

PYTHON := .venv/bin/python

.PHONY: help setup fetch beatport bandcamp spotify bpm analyze audit edit export import render view test print todo slskd download

# slskd (the Soulseek daemon) is usually installed to ~/.local/bin or via a
# downloaded binary; we look for it on PATH first.
SLSKD := $(shell command -v slskd 2>/dev/null)

help:
	@awk '/^#/ { sub(/^# ?/, ""); print; next } { exit }' Makefile

setup:
	@test -x "$(UV)" || curl -LsSf https://astral.sh/uv/install.sh | sh
	@test -d .venv || "$(UV)" venv --python 3.12 .venv
	"$(UV)" pip install -r requirements.txt --python $(PYTHON)
	@test -f .env || cp .env.example .env
	@echo "Ready. Now fill in your data in the .env file and run: make fetch"

fetch:
	$(PYTHON) fetch_discogs.py

beatport:
	$(PYTHON) enrich_beatport.py $(n)

bandcamp:
	$(PYTHON) enrich_bandcamp.py

spotify:
	$(PYTHON) enrich_spotify.py

bpm:
	$(PYTHON) enrich_bpm.py

analyze:
	$(PYTHON) analyze_bpm.py $(n)

audit:
	$(PYTHON) audit_bpm.py $(n)

edit:
	$(PYTHON) edit_bpm.py

# (fallback: the old CSV workflow still works with export/import)
export:
	$(PYTHON) bpm_manual.py export

import:
	$(PYTHON) bpm_manual.py import

render:
	$(PYTHON) render_labels.py $(d)

view:
	$(PYTHON) render_labels.py $(d) --view

test:
	$(PYTHON) print_labels.py $(d) --test

print:
	$(PYTHON) print_labels.py $(d)

# make todo -> runs all automatic steps at once (printing is manual)
todo: fetch beatport bandcamp spotify bpm render

# Start the Soulseek daemon. Keep this running in its own terminal while
# you run "make download" in another. (Download/install slskd first — see
# the README section "Download digital copies (Soulseek)".)
slskd:
	@test -n "$(SLSKD)" || { echo "slskd not found. Install it first — see the README."; exit 1; }
	"$(SLSKD)"

# make download          -> everything still missing
# make download d=aphex  -> only records matching "aphex"
# make download force=1  -> re-download even what's already there
download:
	$(PYTHON) download_music.py $(d) $(if $(force),--force,)
