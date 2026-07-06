"""
fetch_discogs.py — PASO 1

Se conecta a tu cuenta de Discogs, trae TODA tu colección (todos los
vinilos, sin importar en qué carpeta de Discogs los tengas guardados)
y guarda para cada uno: artista, título, año, sello, la lista de
tracks con su posición (A1, A2...), título y duración, y la tapa de
la edición real del vinilo (Discogs es la fuente maestra: lo que
falte acá lo completan después Beatport, Bandcamp y Spotify).

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
from pathlib import Path

import discogs_client
from discogs_client.exceptions import HTTPError

import config
from comunes import bajar_tapa
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

            # Sello y número de catálogo (para la etiqueta), y la fecha
            # de edición del vinilo (más precisa que el año solo, cuando
            # Discogs la tiene: "2024-12-30").
            sello = limpiar_artista(release.labels[0].name) if release.labels else None
            catno = release.labels[0].data.get("catno") if release.labels else None
            if catno in ("none", ""):
                catno = None
            released = release.data.get("released") or None

            cursor.execute(
                """
                INSERT INTO releases (release_id, artist, title, year, sello, catno, released)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(release_id) DO UPDATE SET
                    artist = excluded.artist,
                    title = excluded.title,
                    year = excluded.year,
                    sello = excluded.sello,
                    catno = excluded.catno,
                    released = excluded.released
                """,
                (release.id, artist, release.title, release.year or None, sello, catno, released),
            )

            # La tapa, directo de Discogs (la fuente maestra: es la
            # foto de la edición real del vinilo). El pedido del
            # release ya trae las URLs de las imágenes, así que esto
            # no gasta llamadas extra a la API. Si ya hay tapa bajada,
            # no se pisa (para rehacer una: borrala de covers/).
            cursor.execute("SELECT cover_path FROM releases WHERE release_id = ?", (release.id,))
            tapa_actual = cursor.fetchone()["cover_path"]
            if not tapa_actual or not (Path(__file__).parent / tapa_actual).exists():
                imagenes = release.data.get("images") or []
                principales = [im for im in imagenes if im.get("type") == "primary"] or imagenes
                if principales and principales[0].get("uri"):
                    ruta = bajar_tapa(principales[0]["uri"], release.id)
                    if ruta:
                        cursor.execute(
                            "UPDATE releases SET cover_path = ? WHERE release_id = ?",
                            (ruta, release.id),
                        )
                        print("   tapa bajada de Discogs")

            # Antes de reemplazar los tracks, guardamos lo que ya tenían
            # y no viene de Discogs (BPM y su estado de validación, key,
            # ISRC, duraciones completadas desde otras fuentes), para no
            # perderlo cada vez que actualizás la colección.
            cursor.execute(
                "SELECT position, bpm, bpm_source, bpm_alt, bpm_needs_review, bpm_verified,"
                "       key, key_source, isrc, duration_display"
                " FROM tracks WHERE release_id = ?",
                (release.id,),
            )
            tracks_previos = {row["position"]: dict(row) for row in cursor.fetchall()}

            # Lo mismo con el detalle por fuente (bpm_sources): como los
            # tracks se recrean con id nuevo, lo guardamos por posición
            # y lo volvemos a colgar del id nuevo al insertar.
            cursor.execute(
                "SELECT tracks.position, bpm_sources.source, bpm_sources.bpm, bpm_sources.detail"
                " FROM bpm_sources JOIN tracks ON tracks.id = bpm_sources.track_id"
                " WHERE tracks.release_id = ?",
                (release.id,),
            )
            fuentes_previas = {}
            for row in cursor.fetchall():
                fuentes_previas.setdefault(row["position"], []).append(
                    (row["source"], row["bpm"], row["detail"])
                )

            cursor.execute(
                "DELETE FROM bpm_sources WHERE track_id IN"
                " (SELECT id FROM tracks WHERE release_id = ?)",
                (release.id,),
            )
            cursor.execute("DELETE FROM tracks WHERE release_id = ?", (release.id,))

            for track in release.tracklist:
                # Los renglones sin posición son títulos de sección
                # ("Side A", nombres de suites, etc.), no canciones.
                if not track.position:
                    continue
                previo = tracks_previos.get(track.position) or {}
                # Si Discogs no trae la duración pero ya la habíamos
                # completado desde otra fuente, la mantenemos.
                duracion = track.duration or previo.get("duration_display") or ""

                # En discos "Various" (recopilatorios), Discogs guarda el
                # artista real por track (track.artists) en vez de en el
                # disco. Si el track trae uno, lo guardamos aparte; si no
                # (disco de un solo artista), queda en None y se usa el
                # artista del disco.
                track_artist = (
                    " / ".join(limpiar_artista(a.name) for a in track.artists)
                    if track.artists
                    else None
                )
                if track_artist == artist:
                    track_artist = None

                cursor.execute(
                    """
                    INSERT INTO tracks (release_id, position, title, artist, duration_display,
                                        bpm, bpm_source, bpm_alt, bpm_needs_review, bpm_verified,
                                        key, key_source, isrc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        release.id, track.position, track.title, track_artist, duracion,
                        previo.get("bpm"), previo.get("bpm_source"), previo.get("bpm_alt"),
                        previo.get("bpm_needs_review") or 0, previo.get("bpm_verified") or 0,
                        previo.get("key"), previo.get("key_source"), previo.get("isrc"),
                    ),
                )
                track_id_nuevo = cursor.lastrowid
                for fuente, bpm, detalle in fuentes_previas.get(track.position, []):
                    cursor.execute(
                        "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm, detail)"
                        " VALUES (?, ?, ?, ?)",
                        (track_id_nuevo, fuente, bpm, detalle),
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
            f"DELETE FROM bpm_sources WHERE track_id IN"
            f" (SELECT id FROM tracks WHERE release_id NOT IN ({marcadores}))",
            ids_en_coleccion,
        )
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
    print("Próximo paso: python enrich_beatport.py  (BPM y tonalidad)")


if __name__ == "__main__":
    main()
