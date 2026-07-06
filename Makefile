# Atajos para no tener que acordarse de los comandos.
#
# Uso:
#   make            -> muestra esta ayuda
#   make setup      -> instala todo (una sola vez)
#   make fetch      -> paso 1: colección y tapas desde Discogs
#   make beatport   -> paso 2: BPM y key desde Beatport, para TODOS
#                      los tracks (make beatport n=5 para probar)
#   make bandcamp   -> paso 3: tapas/duraciones que falten (Bandcamp)
#   make spotify    -> paso 4: último respaldo (tapa, duraciones, ISRC)
#   make analizar   -> paso 5: mide lo que Beatport no tuvo, desde
#                      YouTube (make analizar n=5 para probar con 5)
#   make bpm        -> paso 6: última red para BPM (Deezer, opcional)
#   make auditar    -> re-chequea lo medido con la versión vieja
#                      (una sola vez; make auditar n=5 para probar)
#   make editar     -> paso 7: editor y VALIDADOR de BPM y key (la ✓
#                      la ponés vos ahí, viendo todas las fuentes)
#   make render     -> paso 8: genera todas las etiquetas
#   make prueba     -> paso 9 en modo de prueba (sin impresora)
#   make print      -> paso 9: imprime lo pendiente
#
# Los pasos con filtro aceptan d=texto (d de "disco"):
#   make render d=aphex   -> solo discos que contengan "aphex"
#   make ver d=aphex      -> lo mismo + los abre en Vista Previa
#   make prueba d=aphex
#   make print d=aphex
#
# Y para hacer todo de una (fetch + beatport + bandcamp + spotify +
# bpm + render):
#   make todo

# uv vive en ~/.local/bin, que no siempre está en el PATH de make,
# así que lo buscamos y si no, usamos esa ruta directamente.
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
	@echo "Listo. Ahora completá tus datos en el archivo .env y corré: make fetch"

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

# (fallback: el viejo flujo por CSV sigue andando con export/import)
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

# make todo -> corre todos los pasos automáticos de una (el print queda a mano)
todo: fetch beatport bandcamp spotify bpm render
