# Atajos para no tener que acordarse de los comandos.
#
# Uso:
#   make            -> muestra esta ayuda
#   make setup      -> instala todo (una sola vez)
#   make fetch      -> paso 1: trae tu colección de Discogs
#   make bpm        -> paso 2: busca BPM automáticamente
#   make analizar   -> paso 2b: mide los que faltan desde YouTube
#                      (make analizar n=5 para probar con 5)
#   make export     -> paso 3a: exporta CSV con los BPM que faltan
#   make import     -> paso 3b: importa el CSV completado
#   make render     -> paso 4: genera todas las etiquetas
#   make prueba     -> paso 5 en modo de prueba (sin impresora)
#   make print      -> paso 5: imprime lo pendiente
#
# Los pasos con filtro aceptan d=texto (d de "disco"):
#   make render d=aphex   -> solo discos que contengan "aphex"
#   make ver d=aphex      -> lo mismo + los abre en Vista Previa
#   make prueba d=aphex
#   make print d=aphex
#
# Y para hacer todo de una (fetch + bpm + render):
#   make todo

# uv vive en ~/.local/bin, que no siempre está en el PATH de make,
# así que lo buscamos y si no, usamos esa ruta directamente.
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

PYTHON := .venv/bin/python

.PHONY: help setup fetch bpm analizar export import render ver prueba print todo

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

bpm:
	$(PYTHON) enrich_bpm.py

analizar:
	$(PYTHON) analyze_bpm.py $(n)

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

# make todo -> corre fetch + bpm + render de una (el print queda a mano)
todo: fetch bpm render
