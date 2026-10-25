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
#   make render     -> step 7: creates/updates labels after every track's BPM review
#   make print-test -> step 8 in test mode (no printer)
#   make check      -> run the software test suite
#   make backup     -> create a consistent database backup now
#   make print      -> step 8: prints pending labels
#   make print batch=1 -> prints every pending label without pausing
#
# Every automatic step is incremental by default. To deliberately revisit the
# whole collection, add full=1, for example:
#   make fetch full=1
#   make beatport full=1
#   make todo full=1
#
# Steps with filter accept d=text (d for record):
#   make render d=aphex   -> only records containing "aphex"
#   make view d=aphex     -> same + opens them in Preview
#   make print-test d=aphex
#   make print d=aphex
#
# And to do everything automatic at once (including the slow audio fallback):
#   make todo

# uv lives in ~/.local/bin, which is not always in make's PATH,
# so we search for it and if not found, use that path directly.
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

PYTHON := .venv/bin/python

.PHONY: help setup fetch beatport bandcamp spotify analyze audit edit export import render view test print-test check backup print todo

# These targets represent ordered workflows and must remain serial even when a
# caller invokes make with -j.
.NOTPARALLEL: todo test print-test print

help:
	@awk '/^#/ { sub(/^# ?/, ""); print; next } { exit }' Makefile

setup:
	@test -x "$(UV)" || curl -LsSf https://astral.sh/uv/install.sh | sh
	"$(UV)" sync --locked --all-extras --dev
	@test -f .env || cp .env.example .env
	@echo "Ready. Now fill in your data in the .env file and run: make fetch"

fetch:
	$(PYTHON) -m vinyl_labels fetch $(if $(full),--all,)

beatport:
	$(PYTHON) -m vinyl_labels beatport $(n) $(if $(full),--all,)

bandcamp:
	$(PYTHON) -m vinyl_labels bandcamp $(if $(full),--all,)

spotify:
	$(PYTHON) -m vinyl_labels spotify $(if $(full),--all,)

analyze:
	$(PYTHON) -m vinyl_labels analyze $(n) $(if $(full),--all,) $(if $(pace),--pace $(pace),)

audit:
	$(PYTHON) -m vinyl_labels audit $(n)

edit:
	$(PYTHON) -m vinyl_labels edit

# (fallback: the old CSV workflow still works with export/import)
export:
	$(PYTHON) -m vinyl_labels export

import:
	$(PYTHON) -m vinyl_labels import

render:
	$(PYTHON) -m vinyl_labels render $(d) $(if $(full),--all,)

view:
	$(PYTHON) -m vinyl_labels render $(d) --view $(if $(full),--all,)

print-test: render
	$(PYTHON) -m vinyl_labels print $(d) --test

# Backwards-compatible alias: historically `make test` meant a printer dry run.
test: print-test

check:
	$(PYTHON) -m vinyl_labels check

backup:
	$(PYTHON) -m vinyl_labels backup

print: render
	$(PYTHON) -m vinyl_labels print $(d) $(if $(batch),--batch,)

# Run the actual pipeline through the serial, fail-fast command orchestrator.
todo:
	$(PYTHON) -m vinyl_labels workflow $(if $(full),--all,) $(if $(n),--limit $(n),) $(if $(pace),--pace $(pace),)
