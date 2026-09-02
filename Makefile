# Shortcuts so you don't have to remember the commands.
#
# Usage:
#   make            -> show this help
#   make setup      -> install everything (one time only)
#   make fetch      -> step 1: NEW collection records and covers from Discogs
#   make beatport   -> step 2: BPM and key from Beatport, for NEW
#                      tracks (make beatport n=5 to test)
#   make bandcamp   -> step 3: missing covers/durations (Bandcamp)
#   make spotify    -> step 4: final fallback (cover, durations, ISRC)
#   make analyze    -> step 5: measures missing BPM and key from audio
#                      (make analyze n=5 pace=8 for 5 tracks, 8s apart)
#   make audit      -> re-checks measurements from old version
#                      (one time only; make audit n=5 to test)
#   make edit       -> step 6: review fallback BPM/key detector results
#   make render     -> step 7: creates/updates 100%-validated labels
#   make test       -> step 8 in test mode (no printer)
#   make print      -> step 8: prints pending labels
#   make print batch=1 -> prints every pending label without pausing
#
# Every automatic step is incremental by default. To deliberately revisit the
# whole collection, add full=1, for example:
#   make fetch full=1
#   make beatport full=1
#   make todo full=1
#
# Optional, separate from the labels: download a digital copy of the
# collection from Soulseek (needs the slskd daemon, see the README).
#   make slskd      -> make sure the slskd daemon is running (Docker
#                      container or binary)
#   make slskd-status -> is slskd logged in to Soulseek right now? (one shot)
#   make slskd-watch  -> keep watching: notifies you (and restarts slskd to
#                      reset its reconnect backoff) if it drops off Soulseek
#   make download   -> download everything still missing into ~/Music/Vinyl
#                      (make download d=aphex for one record; add force=1
#                      to re-download; retry=1 to re-try tracks that recently
#                      came up empty; deep=1 for a wider, slower search;
#                      j=12 to search 12 records at a time, default 8)
#
# Steps with filter accept d=text (d for record):
#   make render d=aphex   -> only records containing "aphex"
#   make view d=aphex     -> same + opens them in Preview
#   make test d=aphex
#   make print d=aphex
#
# And to do everything at once (fetch + beatport + bandcamp + spotify + render):
#   make todo

# uv lives in ~/.local/bin, which is not always in make's PATH,
# so we search for it and if not found, use that path directly.
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

PYTHON := .venv/bin/python

.PHONY: help setup fetch beatport bandcamp spotify analyze audit edit export import render view test print todo slskd slskd-status slskd-watch download

# slskd (the Soulseek daemon) is usually installed to ~/.local/bin or via a
# downloaded binary; we look for it on PATH first.
SLSKD := $(shell command -v slskd 2>/dev/null)

# docker sometimes isn't in make's PATH (same story as uv above), so fall
# back to its usual location on Mac.
DOCKER := $(shell command -v docker 2>/dev/null || echo /usr/local/bin/docker)

help:
	@awk '/^#/ { sub(/^# ?/, ""); print; next } { exit }' Makefile

setup:
	@test -x "$(UV)" || curl -LsSf https://astral.sh/uv/install.sh | sh
	@test -d .venv || "$(UV)" venv --python 3.12 .venv
	"$(UV)" pip install -r requirements.txt --python $(PYTHON)
	@test -f .env || cp .env.example .env
	@echo "Ready. Now fill in your data in the .env file and run: make fetch"

fetch:
	$(PYTHON) fetch_discogs.py $(if $(full),--all,)

beatport:
	$(PYTHON) enrich_beatport.py $(n) $(if $(full),--all,)

bandcamp:
	$(PYTHON) enrich_bandcamp.py $(if $(full),--all,)

spotify:
	$(PYTHON) enrich_spotify.py $(if $(full),--all,)

analyze:
	$(PYTHON) analyze_bpm.py $(n) $(if $(full),--all,) $(if $(pace),--pace $(pace),)

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
	$(PYTHON) render_labels.py $(d) $(if $(full),--all,)

view:
	$(PYTHON) render_labels.py $(d) --view $(if $(full),--all,)

test: render
	$(PYTHON) print_labels.py $(d) --test

print: render
	$(PYTHON) print_labels.py $(d) $(if $(batch),--batch,)

# make todo -> runs all automatic steps at once (printing is manual)
todo: fetch beatport bandcamp spotify render

# Start the Soulseek daemon. With Docker (recommended, container named
# "slskd") this just makes sure it's running — it stays up in the
# background. With the binary, keep this open in its own terminal while
# you run "make download" in another. (Setup: see the README section
# "Download digital copies (Soulseek)".)
slskd:
	@if [ -n "$(SLSKD)" ]; then \
		"$(SLSKD)"; \
	elif ! [ -x "$(DOCKER)" ]; then \
		echo "slskd not found (no binary, no Docker). See the README."; exit 1; \
	elif ! "$(DOCKER)" info >/dev/null 2>&1; then \
		echo "Docker itself isn't running. Open Docker Desktop, wait for it to start, then retry."; exit 1; \
	elif "$(DOCKER)" inspect slskd >/dev/null 2>&1; then \
		"$(DOCKER)" start slskd >/dev/null && \
		echo "slskd is running in Docker — web UI: http://localhost:5030 (logs: docker logs -f slskd)"; \
	else \
		echo "No 'slskd' Docker container yet. See the README section about Soulseek."; exit 1; \
	fi

# Is slskd actually logged in to Soulseek? (Docker's own health check only
# proves the web UI is up — it stays green while slskd is stuck offline.)
#   make slskd-status  -> print the state once and exit
#   make slskd-watch   -> watch continuously; notify + restart if it gets stuck
slskd-status:
	$(PYTHON) slskd_monitor.py

slskd-watch:
	$(PYTHON) slskd_monitor.py --watch

# make download          -> everything still missing
# make download d=aphex  -> only records matching "aphex"
# make download force=1  -> re-download even what's already there
# make download retry=1  -> also re-try tracks that recently came up empty
# make download deep=1   -> also try a title-only search (wider, slower)
# make download j=12     -> search 12 records at a time (default 8)
download:
	$(PYTHON) download_music.py $(d) $(if $(force),--force,) $(if $(retry),--retry-failed,) $(if $(deep),--deep,) $(if $(j),--parallel $(j),)
