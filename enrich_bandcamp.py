"""
enrich_bandcamp.py — PASO 3

Respaldo de Discogs para lo que todavía falte, pensado para música
underground y de sellos chicos (que en Bandcamp suele estar toda):

  - La tapa del disco, si el paso 1 no la consiguió de Discogs.
  - Las duraciones de los tracks que Discogs trajo vacías (muy común
    en ediciones de vinilo, donde nadie las cargó).

Usa la API pública del buscador de bandcamp.com (la misma que usa la
lupa del sitio, sin clave) para encontrar el álbum, y de la página
del álbum saca las duraciones exactas y la tapa. Bandcamp no publica
BPM ni tonalidad: para eso están los otros pasos.

Cómo correrlo:
    python enrich_bandcamp.py

Se puede correr las veces que quieras: los discos que ya están
completos se saltean sin gastar pedidos.
"""

import html
import json
import re
import time
from pathlib import Path

import requests

from comunes import bajar_tapa, formatear_duracion, se_parecen
from db import get_connection, init_db

# API pública (sin key) que usa el buscador de bandcamp.com.
BANDCAMP_SEARCH_API = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

NAVEGADOR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def buscar_album_bandcamp(artista, titulo):
    """Busca el álbum en Bandcamp y devuelve el resultado (dict con
    item_url_path, name, band_name...) solo si artista y título
    realmente coinciden, o None."""
    es_various = artista.lower().startswith("various")
    consulta = titulo if es_various else f"{artista} {titulo}"
    try:
        resp = requests.post(
            BANDCAMP_SEARCH_API,
            json={
                "search_text": consulta,
                "search_filter": "a",  # a = álbumes
                "full_page": False,
                "fan_id": None,
            },
            timeout=15,
        )
        resp.raise_for_status()
        resultados = resp.json().get("auto", {}).get("results") or []
    except (requests.RequestException, ValueError):
        return None

    for r in resultados:
        if r.get("type") != "a" or not r.get("item_url_path"):
            continue
        if not se_parecen(r.get("name", ""), titulo):
            continue
        # El "band_name" de Bandcamp a veces es el sello y no el
        # artista; por eso lo chequeamos, pero solo si no es un
        # recopilatorio (ahí el artista no sirve para comparar).
        if not es_various and not se_parecen(r.get("band_name", ""), artista, umbral=0.8):
            continue
        return r
    return None


def leer_pagina_album(url):
    """Baja la página del álbum y devuelve (trackinfo, url_tapa):
    la lista de tracks con duraciones (del JSON data-tralbum que
    Bandcamp incrusta para su reproductor) y la URL de la tapa."""
    try:
        pagina = requests.get(url, headers=NAVEGADOR, timeout=20).text
    except requests.RequestException:
        return [], None

    trackinfo = []
    datos = re.search(r'data-tralbum="([^"]+)"', pagina)
    if datos:
        try:
            trackinfo = json.loads(html.unescape(datos.group(1))).get("trackinfo") or []
        except ValueError:
            pass

    tapa = re.search(r'<meta property="og:image" content="([^"]+)"', pagina)
    return trackinfo, tapa.group(1) if tapa else None


def main():
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()
    print(f"Discos en la colección: {len(releases)}\n")

    stats = {"tapas": 0, "duraciones": 0, "sin_bandcamp": 0}
    for i, release in enumerate(releases, start=1):
        rid = release["release_id"]
        cursor.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (rid,))
        tracks_db = cursor.fetchall()

        falta_tapa = not release["cover_path"] or not (Path(__file__).parent / release["cover_path"]).exists()
        faltan_duraciones = any(not t["duration_display"] for t in tracks_db)
        if not falta_tapa and not faltan_duraciones:
            continue  # ya está completo, ni gastamos pedidos

        etiqueta = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
        artista = release["artist"].split(" / ")[0]
        album = buscar_album_bandcamp(artista, release["title"])
        novedades = []

        if album:
            time.sleep(0.5)  # entre la búsqueda y la página, sin apuro
            trackinfo, url_tapa = leer_pagina_album(album["item_url_path"])

            if faltan_duraciones and trackinfo:
                duraciones = 0
                for t in tracks_db:
                    if t["duration_display"]:
                        continue
                    match = next(
                        (b for b in trackinfo if b.get("duration") and se_parecen(b.get("title", ""), t["title"])),
                        None,
                    )
                    if match:
                        cursor.execute(
                            "UPDATE tracks SET duration_display = ? WHERE id = ?",
                            (formatear_duracion(match["duration"]), t["id"]),
                        )
                        duraciones += 1
                stats["duraciones"] += duraciones
                if duraciones:
                    novedades.append(f"{duraciones} duraciones")

            if falta_tapa and url_tapa:
                ruta = bajar_tapa(url_tapa, rid)
                if ruta:
                    cursor.execute("UPDATE releases SET cover_path = ? WHERE release_id = ?", (ruta, rid))
                    stats["tapas"] += 1
                    novedades.append("tapa")
        else:
            stats["sin_bandcamp"] += 1
            novedades.append("no está en Bandcamp")

        conn.commit()
        print(f"{etiqueta}: {', '.join(novedades) if novedades else 'sin novedades'}")
        time.sleep(0.5)

    conn.close()

    print("\n" + "=" * 50)
    print(f"Tapas bajadas: {stats['tapas']} | duraciones completadas: {stats['duraciones']}")
    if stats["sin_bandcamp"]:
        print(f"Discos que no están en Bandcamp: {stats['sin_bandcamp']}.")
    print("Próximo paso: python enrich_spotify.py  (último respaldo, opcional)")
    print("           o: python enrich_bpm.py      (BPM que falten)")


if __name__ == "__main__":
    main()
