"""
enrich_bpm.py — PASO 2

Para cada track que todavía no tiene BPM guardado, lo busca
automáticamente en internet:

  1. Primero en Deezer, que es gratis y no pide ninguna clave.
  2. Si no aparece y configuraste GETSONGBPM_API_KEY en config.py,
     prueba también en getsongbpm.com.

Esto es "mejor esfuerzo": vinilos raros, ediciones de nicho, remixes
o tracks instrumentales sin nombre suelen no aparecer. Los que no se
encuentren quedan con BPM vacío, y los completás a mano después con
bpm_manual.py — es totalmente normal tener que hacerlo para una
parte de tu colección.

Cómo correrlo:
    python enrich_bpm.py
"""

import time
import urllib.parse

import requests

import config
from db import get_connection, init_db

DEEZER_API = "https://api.deezer.com"
GETSONGBPM_API = "https://api.getsong.co"


def buscar_bpm_deezer(titulo, artista):
    """
    Busca un track en Deezer por título + artista y devuelve su BPM
    (float) o None. Deezer no pide API key, pero el BPM solo viene
    en el detalle del track, así que son dos pedidos: buscar y pedir
    el detalle del primer resultado.

    La búsqueda "estricta" de Deezer se saltea varios tracks que la
    búsqueda simple sí encuentra, así que probamos las dos. En la
    simple chequeamos que el artista coincida, para no traer el BPM
    de otra canción con el mismo nombre.
    """
    try:
        track_id = None
        # 1) Búsqueda estricta por artista + título exactos.
        resp = requests.get(
            f"{DEEZER_API}/search",
            params={"q": f'artist:"{artista}" track:"{titulo}"', "limit": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            resultados = resp.json().get("data") or []
            if resultados:
                track_id = resultados[0]["id"]

        # 2) Si no hubo suerte, búsqueda simple, verificando el artista.
        if track_id is None:
            time.sleep(0.3)  # Deezer permite ~50 pedidos cada 5 segundos
            resp = requests.get(
                f"{DEEZER_API}/search",
                params={"q": f"{artista} {titulo}", "limit": 5},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            for resultado in resp.json().get("data") or []:
                if artista.lower() in resultado["artist"]["name"].lower():
                    track_id = resultado["id"]
                    break
            if track_id is None:
                return None

        time.sleep(0.3)

        resp = requests.get(f"{DEEZER_API}/track/{track_id}", timeout=10)
        if resp.status_code != 200:
            return None
        bpm = resp.json().get("bpm")
    except (requests.RequestException, ValueError, KeyError):
        return None

    # Deezer devuelve 0 cuando no analizó el tema.
    if not bpm:
        return None
    return float(bpm)


def buscar_bpm_getsongbpm(titulo, artista):
    """
    Busca un track en getsongbpm.com por título + artista.
    Devuelve el BPM (float) si lo encuentra, o None si no.
    """
    lookup = f"song:{titulo} artist:{artista}"
    params = {
        "api_key": config.GETSONGBPM_API_KEY,
        "type": "both",
        "lookup": lookup,
        "limit": 1,
    }
    url = f"{GETSONGBPM_API}/search/?{urllib.parse.urlencode(params)}"

    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None
    resultados = data.get("search") or []
    if not resultados:
        return None

    tempo = resultados[0].get("tempo")
    try:
        return float(tempo)
    except (TypeError, ValueError):
        return None


def main():
    init_db()  # por si todavía no corriste ningún otro paso
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT tracks.id, tracks.title, releases.artist
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm IS NULL
        """
    )
    pendientes = cursor.fetchall()

    print(f"Tracks sin BPM: {len(pendientes)}\n")
    if not config.GETSONGBPM_API_KEY:
        print(
            "(GETSONGBPM_API_KEY no está configurada: busco solo en Deezer.\n"
            " Es opcional, Deezer suele alcanzar.)\n"
        )

    encontrados = {"deezer": 0, "getsongbpm": 0}
    for i, row in enumerate(pendientes, start=1):
        # Si el disco tiene varios artistas los guardamos como
        # "Artista 1 / Artista 2"; para buscar usamos solo el primero.
        artista = row["artist"].split(" / ")[0]

        bpm = buscar_bpm_deezer(row["title"], artista)
        fuente = "deezer"

        if not bpm and config.GETSONGBPM_API_KEY:
            bpm = buscar_bpm_getsongbpm(row["title"], artista)
            fuente = "getsongbpm"

        if bpm:
            cursor.execute(
                "UPDATE tracks SET bpm = ?, bpm_source = ? WHERE id = ?",
                (bpm, fuente, row["id"]),
            )
            conn.commit()
            encontrados[fuente] += 1
            print(f"[{i}/{len(pendientes)}] OK  {row['artist']} - {row['title']} -> {bpm:g} BPM ({fuente})")
        else:
            print(f"[{i}/{len(pendientes)}] --  {row['artist']} - {row['title']} (no encontrado)")

        time.sleep(0.3)

    conn.close()

    total_ok = sum(encontrados.values())
    print("\n" + "=" * 50)
    print(f"BPM encontrado automáticamente para {total_ok} de {len(pendientes)} tracks")
    print(f"(Deezer: {encontrados['deezer']}, getsongbpm: {encontrados['getsongbpm']}).")
    print("Para completar el resto a mano, corré: python bpm_manual.py export")


if __name__ == "__main__":
    main()
