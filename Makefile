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
#   make analizar   -> step 5: measures what Beatport didn't have, from
#                      YouTube (make analizar n=5 to test with 5)
#   make bpm        -> step 6: last resort for BPM (Deezer, optional)
#   make auditar    -> re-checks measurements from old version
#                      (one time only; make auditar n=5 to test)
#   make editar     -> step 7: BPM and key editor and VALIDATOR (you put
#                      the ✓ there, seeing all sources)
#   make render     -> step 8: generates all labels
#   make prueba     -> step 9 in test mode (no printer)
#   make print      -> step 9: prints pending labels
#
# Steps with filter accept d=text (d for "disco"/record):
#   make render d=aphex   -> only records containing "aphex"
#   make ver d=aphex      -> same + opens them in Preview
#   make prueba d=aphex
#   make print d=aphex
#
# And to do everything at once (fetch + beatport + bandcamp + spotify +
# bpm + render):
#   make todo

# uv lives in ~/.local/bin, which is not always in make's PATH,
# so we search for it and if not found, use that path directly.
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

PYTHON := .venv/bin/python

.PHONY: help setup fetch beatport bandcamp spotify bpm analizar auditar editar export import render ver prueba print todo

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

analizar:
	$(PYTHON) analyze_bpm.py $(n)

auditar:
	$(PYTHON) audit_bpm.py $(n)

editar:
	$(PYTHON) edit_bpm.py

# (fallback: the old CSV workflow still works with export/import)
export:
	$(PYTHON) bpm_manual.py export

import:
	$(PYTHON) bpm_manual.py import

render:
	$(PYTHON) render_labels.py $(d)

ver:
	$(PYTHON) render_labels.py $(d) --ver

prueba:
	$(PYTHON) print_labels.py $(d) --prueba

print:
	$(PYTHON) print_labels.py $(d)

# make todo -> runs all automatic steps at once (printing is manual)
todo: fetch beatport bandcamp spotify bpm render
