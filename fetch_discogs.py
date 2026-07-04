"""
fetch_discogs.py — PASO 1

Se conecta a tu cuenta de Discogs, trae TODA tu colección (todos los
vinilos, sin importar en qué carpeta de Discogs los tengas guardados)
y guarda para cada uno: artista, título, año, y la lista de tracks
con su posición (A1, A2...), título y duración.

Cómo correrlo:
    python fetch_discogs.py

Podés correrlo las veces que quieras: los discos ya guardados se
actualizan (sin perder los BPM que hayas cargado), los nuevos se
agregan, y los que hayas sacado de tu colección se borran.

Ojo: Discogs limita los pedidos a 60 por minuto, así que con una
colección grande este paso tarda — más o menos 1 segundo por disco.
"""

import re
import time

import discogs_client
from discogs_client.exceptions import HTTPError

import config
from db import get_connection, init_db


def limpiar_artista(nombre):
    """Discogs agrega sufijos como "Aphex Twin (2)" para distinguir
    artistas con el mismo nombre. En una etiqueta eso no aporta nada
    y arruina la búsqueda de BPM, así que lo sacamos."""
    return re.sub(r"\s\(\d+\)$", "", nombre)


def con_reintento(funcion, intentos=3):
    """Ejecuta una llamada a Discogs; si responde 429 (demasiados
    pedidos), espera lo que pida el servidor y reintenta."""
    for intento in range(intentos):
        try:
            return funcion()
        except HTTPError as e:
            if e.status_code == 429 and intento < intentos - 1:
                espera = 60
                print(f"   Discogs pide esperar... pauso {espera}s y sigo.")
                time.sleep(espera)
            else:
                raise


def main():
    if not config.DISCOGS_USER_TOKEN:
        print(
            "Falta tu token de Discogs.\n"
            "Copiá .env.example como .env (si no existe ya) y completá\n"
            "DISCOGS_USER_TOKEN con el token que te da\n"
            "https://www.discogs.com/settings/developers"
        )
        return

    init_db()  # crea las tablas si es la primera vez

    print("Conectando con Discogs...")
    d = discogs_client.Client(
        config.DISCOGS_USER_AGENT,
        user_token=config.DISCOGS_USER_TOKEN,
    )

    me = d.identity()
    print(f"Conectado como: {me.username}\n")

    # La carpeta 0 ("All") siempre contiene TODA tu colección,
    # sin importar cómo la hayas organizado en subcarpetas.
    all_folder = me.collection_folders[0]
    total = all_folder.count
    print(f"Discos encontrados en tu colección: {total}\n")

    conn = get_connection()
    cursor = conn.cursor()

    errores = []
    ids_en_coleccion = []

    for i, item in enumerate(all_folder.releases, start=1):
        try:
            release = item.release
            # El pedido a Discogs recién se hace acá (con reintento
            # si nos topamos con el límite de pedidos por minuto).
            con_reintento(release.refresh)
            ids_en_coleccion.append(release.id)

            artist = (
                " / ".join(limpiar_artista(a.name) for a in release.artists)
                if release.artists
                else "Desconocido"
            )

            print(f"[{i}/{total}] {artist} — {release.title}")

            cursor.execute(
                """
                INSERT INTO releases (release_id, artist, title, year)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(release_id) DO UPDATE SET
                    artist = excluded.artist,
                    title = excluded.title,
                    year = excluded.year
                """,
                (release.id, artist, release.title, release.year or None),
            )

            # Antes de reemplazar los tracks, guardamos los BPM que ya
            # tenían (los cargados a mano o por API), para no perderlos
            # cada vez que actualizás la colección.
            cursor.execute(
                "SELECT position, bpm, bpm_source FROM tracks WHERE release_id = ? AND bpm IS NOT NULL",
                (release.id,),
            )
            bpm_guardados = {row["position"]: (row["bpm"], row["bpm_source"]) for row in cursor.fetchall()}

            cursor.execute("DELETE FROM tracks WHERE release_id = ?", (release.id,))

            for track in release.tracklist:
                # Los renglones sin posición son títulos de sección
                # ("Side A", nombres de suites, etc.), no canciones.
                if not track.position:
                    continue
                bpm, bpm_source = bpm_guardados.get(track.position, (None, None))
                cursor.execute(
                    """
                    INSERT INTO tracks (release_id, position, title, duration_display, bpm, bpm_source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (release.id, track.position, track.title, track.duration or "", bpm, bpm_source),
                )

            conn.commit()

        except Exception as e:
            # Si un disco puntual falla (por ejemplo, un problema de
            # red momentáneo), lo anotamos y seguimos con el resto
            # en vez de cortar todo el proceso.
            errores.append((getattr(item, "id", "?"), str(e)))
            print(f"   -> Error con este disco, sigo con el resto: {e}")

        # Discogs permite 60 pedidos por minuto. Esta pausa evita
        # que te bloqueen temporalmente si tenés una colección grande.
        time.sleep(1.1)

    # Si el recorrido terminó sin errores, borramos los discos que ya
    # no están en tu colección de Discogs (los vendiste, etc.).
    # Si hubo errores no borramos nada, por las dudas.
    if not errores and ids_en_coleccion:
        marcadores = ",".join("?" * len(ids_en_coleccion))
        cursor.execute(
            f"DELETE FROM tracks WHERE release_id NOT IN ({marcadores})",
            ids_en_coleccion,
        )
        cursor.execute(
            f"DELETE FROM releases WHERE release_id NOT IN ({marcadores})",
            ids_en_coleccion,
        )
        if cursor.rowcount:
            print(f"\nSaqué {cursor.rowcount} discos que ya no están en tu colección.")
        conn.commit()

    conn.close()

    print("\n" + "=" * 50)
    print(f"Listo. {len(ids_en_coleccion)} discos guardados correctamente.")
    if errores:
        print(f"{len(errores)} discos tuvieron errores (revisá arriba).")
    print("Próximo paso: python enrich_bpm.py")


if __name__ == "__main__":
    main()
