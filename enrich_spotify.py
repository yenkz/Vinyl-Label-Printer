"""
enrich_spotify.py — PASO 4 (opcional)

Último respaldo de la cadena de fuentes (Discogs → Beatport →
Bandcamp → Spotify): completa desde la API de Spotify lo que
todavía siga faltando después de los pasos anteriores:

  - La tapa del disco, si ni Discogs ni Bandcamp la tuvieron.
  - Las duraciones de los tracks que sigan vacías.
  - El código ISRC de cada track (queda guardado en la base como
    identificador; no se imprime).

Ojo: las apps de Spotify creadas después de nov 2024 NO tienen
acceso al BPM (el endpoint audio-features devuelve 403), así que
esto no reemplaza a enrich_beatport.py / enrich_bpm.py.

Cómo correrlo:
    python enrich_spotify.py

Se puede correr las veces que quieras: lo ya enriquecido se saltea,
así que las corridas siguientes son rápidas.
"""

import time
from pathlib import Path

import requests

import config
from comunes import bajar_tapa, se_parecen
from db import get_connection, init_db

SPOTIFY_ACCOUNTS = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"


def obtener_token_spotify():
    """Pide un token de app (client credentials). Dura 1 hora, de
    sobra para toda la corrida."""
    resp = requests.post(
        SPOTIFY_ACCOUNTS,
        data={"grant_type": "client_credentials"},
        auth=(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def buscar_album_spotify(headers, artista, titulo):
    """Busca el disco en Spotify y devuelve el álbum (dict) solo si
    artista y título realmente coinciden, o None."""
    # En los recopilatorios ("Various") el artista no sirve para buscar.
    es_various = artista.lower().startswith("various")
    consulta = titulo if es_various else f"{artista} {titulo}"
    try:
        resp = requests.get(
            f"{SPOTIFY_API}/search",
            params={"q": consulta, "type": "album", "limit": 5},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        candidatos = resp.json().get("albums", {}).get("items") or []
    except requests.RequestException:
        return None

    for album in candidatos:
        if not se_parecen(album["name"], titulo):
            continue
        if es_various:
            return album
        nombres = [a["name"] for a in album.get("artists", [])]
        if any(se_parecen(n, artista, umbral=0.8) for n in nombres):
            return album
    return None


def tracks_del_album_spotify(headers, album_id):
    """Trae los tracks del álbum con duración e ISRC. El endpoint de
    varios tracks juntos (/tracks?ids=...) está bloqueado para las
    apps nuevas, así que hay que pedirlos de a uno."""
    try:
        resp = requests.get(f"{SPOTIFY_API}/albums/{album_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        items = resp.json().get("tracks", {}).get("items") or []
    except requests.RequestException:
        return []

    tracks = []
    for item in items:
        try:
            resp = requests.get(f"{SPOTIFY_API}/tracks/{item['id']}", headers=headers, timeout=15)
            if resp.status_code != 200:
                continue
            t = resp.json()
        except requests.RequestException:
            continue
        segundos = (t.get("duration_ms") or 0) // 1000
        tracks.append(
            {
                "titulo": t.get("name") or "",
                "duracion": f"{segundos // 60}:{segundos % 60:02d}" if segundos else "",
                "isrc": (t.get("external_ids") or {}).get("isrc"),
            }
        )
        time.sleep(0.2)
    return tracks


def main():
    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        print(
            "Faltan SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET en el .env.\n"
            "Creá una app gratis en https://developer.spotify.com/dashboard\n"
            "y pegá acá sus credenciales. (Este paso es opcional: sin esto,\n"
            "las etiquetas salen igual, pero sin tapa ni duraciones extra.)"
        )
        return

    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    token = obtener_token_spotify()
    headers = {"Authorization": f"Bearer {token}"}

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()
    print(f"Discos en la colección: {len(releases)}\n")

    stats = {"tapas": 0, "duraciones": 0, "isrc": 0, "sin_spotify": 0}
    for i, release in enumerate(releases, start=1):
        rid = release["release_id"]
        cursor.execute("SELECT * FROM tracks WHERE release_id = ? ORDER BY id", (rid,))
        tracks_db = cursor.fetchall()

        falta_tapa = not release["cover_path"] or not (Path(__file__).parent / release["cover_path"]).exists()
        faltan_datos = any(not t["duration_display"] or not t["isrc"] for t in tracks_db)
        if not falta_tapa and not faltan_datos:
            continue  # ya está completo, ni gastamos pedidos

        etiqueta = f"[{i}/{len(releases)}] {release['artist']} - {release['title']}"
        artista = release["artist"].split(" / ")[0]
        album = buscar_album_spotify(headers, artista, release["title"])
        novedades = []

        if album:
            if falta_tapa and album.get("images"):
                ruta = bajar_tapa(album["images"][0]["url"], rid)
                if ruta:
                    cursor.execute("UPDATE releases SET cover_path = ? WHERE release_id = ?", (ruta, rid))
                    stats["tapas"] += 1
                    novedades.append("tapa")

            if faltan_datos:
                tracks_spotify = tracks_del_album_spotify(headers, album["id"])
                duraciones = isrcs = 0
                for t in tracks_db:
                    match = next((s for s in tracks_spotify if se_parecen(s["titulo"], t["title"])), None)
                    if not match:
                        continue
                    if not t["duration_display"] and match["duracion"]:
                        cursor.execute(
                            "UPDATE tracks SET duration_display = ? WHERE id = ?",
                            (match["duracion"], t["id"]),
                        )
                        duraciones += 1
                    if not t["isrc"] and match["isrc"]:
                        cursor.execute("UPDATE tracks SET isrc = ? WHERE id = ?", (match["isrc"], t["id"]))
                        isrcs += 1
                stats["duraciones"] += duraciones
                stats["isrc"] += isrcs
                if duraciones:
                    novedades.append(f"{duraciones} duraciones")
                if isrcs:
                    novedades.append(f"{isrcs} ISRC")
        else:
            stats["sin_spotify"] += 1
            novedades.append("no está en Spotify")

        conn.commit()
        print(f"{etiqueta}: {', '.join(novedades) if novedades else 'sin novedades'}")
        time.sleep(0.2)

    conn.close()

    print("\n" + "=" * 50)
    print(
        f"Tapas bajadas: {stats['tapas']} | duraciones completadas: {stats['duraciones']} | "
        f"ISRC guardados: {stats['isrc']}"
    )
    if stats["sin_spotify"]:
        print(f"Discos que no están en Spotify: {stats['sin_spotify']} (normal con vinilos de nicho).")
    print("Próximo paso: python enrich_bpm.py  (si hay BPM pendientes)")
    print("           o: python render_labels.py")


if __name__ == "__main__":
    main()
