"""
audit_bpm.py — Revisión de los BPM medidos con la versión vieja.

Antes, analyze_bpm.py medía el tempo con un solo detector (librosa),
que en música electrónica a veces se engancha a un pulso que no es:
mide 89 donde el tempo real es 134 (un error de 2/3 que el ajuste de
rango no puede corregir, porque los dos números son tempos válidos).

Este script agarra todos los tracks con BPM automático que todavía
no re-mediste con la versión nueva (los de la versión vieja y también
los que vinieron de Deezer), vuelve a bajar el audio y los re-mide
con los dos detectores:

  - si la medición nueva coincide con la guardada, la re-medición
    queda anotada en bpm_sources: en el editor vas a ver las dos
    fuentes de acuerdo y lo validás con un click (nada se valida
    solo: la ✓ verde siempre la ponés vos);
  - si no coincide, guarda el valor nuevo (deeprhythm, mucho más
    preciso en electrónica) y deja el viejo como alternativa, con el
    track marcado como dudoso para que lo resuelvas en el editor
    (python edit_bpm.py) — un click y listo.

Se puede correr las veces que quieras: los ya re-medidos con los dos
detectores no se re-chequean (eso incluye todo lo que analice
analyze_bpm.py de acá en adelante).

Cómo correrlo:
    python audit_bpm.py        # revisa todos
    python audit_bpm.py 5      # solo 5 (para probar)

Se puede cortar con Ctrl+C: lo ya revisado queda guardado.
"""

import sys
import tempfile
import time

from analyze_bpm import TOLERANCIA_BPM, analizar_track, parsear_duracion
from db import get_connection, init_db, registrar_bpm_fuente


def main():
    limite = None
    if len(sys.argv) > 1:
        try:
            limite = int(sys.argv[1])
        except ValueError:
            print(__doc__)
            return

    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    # Re-medimos los BPM automáticos que no tienen todavía una medición
    # "de la era nueva": las filas de bpm_sources con detail (el video
    # del que salió el número) las escribió el análisis de dos
    # detectores; las que no lo tienen vienen de la versión vieja.
    cursor.execute(
        """
        SELECT tracks.id, tracks.title, tracks.duration_display, tracks.bpm,
               COALESCE(tracks.artist, releases.artist) AS artist,
               releases.catno
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm_source IN ('youtube', 'deezer')
          AND tracks.bpm IS NOT NULL
          AND tracks.bpm_needs_review = 0
          AND tracks.bpm_verified = 0
          AND NOT EXISTS (SELECT 1 FROM bpm_sources
                          WHERE track_id = tracks.id AND source = 'youtube'
                            AND bpm IS NOT NULL AND detail IS NOT NULL)
        ORDER BY releases.artist, releases.title, tracks.id
        """
    )
    pendientes = cursor.fetchall()
    if limite:
        pendientes = pendientes[:limite]

    print(f"Tracks a revisar: {len(pendientes)}")
    print("(vuelve a bajar cada audio y lo re-mide; tarda ~30s por track,")
    print(" podés cortar con Ctrl+C y retomar después)\n")

    confirmados = 0
    corregidos = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pendientes, start=1):
            etiqueta = f"[{i}/{len(pendientes)}] {row['artist']} - {row['title']}"
            try:
                nuevo, _, _, detalle = analizar_track(
                    row["artist"], row["title"],
                    parsear_duracion(row["duration_display"]), tmpdir,
                    row["catno"],
                )
            except KeyboardInterrupt:
                print("\nCortado. Lo revisado hasta acá quedó guardado.")
                break
            except Exception as e:
                print(f"{etiqueta}\n   -> error, sigo con el resto: {e}")
                continue

            if nuevo is None:
                print(f"{etiqueta}\n   -> no pude re-medir ({detalle}), queda como está")
            elif abs(nuevo - row["bpm"]) <= TOLERANCIA_BPM:
                # Dos mediciones independientes coinciden: queda anotado
                # (en el editor lo validás vos con un click).
                registrar_bpm_fuente(conn, row["id"], "youtube", nuevo, detalle)
                conn.commit()
                confirmados += 1
                print(f"{etiqueta} -> {row['bpm']:g} BPM coincide (validalo en el editor)")
            else:
                # La medición vieja era el error típico: guardamos la
                # nueva y dejamos la vieja a un click en el editor.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_alt = ?, bpm_needs_review = 1,"
                    " bpm_verified = 0 WHERE id = ?",
                    (nuevo, row["bpm"], row["id"]),
                )
                registrar_bpm_fuente(conn, row["id"], "youtube", nuevo, detalle)
                conn.commit()
                corregidos += 1
                print(f"{etiqueta} -> {row['bpm']:g} parece mal medido: "
                      f"ahora {nuevo:g} BPM (confirmalo en edit_bpm.py)")

            time.sleep(3)  # pausa para no despertar al anti-bot de YouTube

    conn.close()
    print("\n" + "=" * 50)
    print(f"Coinciden: {confirmados} · Corregidos: {corregidos}")
    if corregidos:
        print("Los corregidos quedaron marcados como dudosos: dales un")
        print("vistazo con: python edit_bpm.py (filtro 'solo dudosos')")
    if confirmados:
        print("Los que coinciden quedaron listos para validar con un click")
        print("en el editor: python edit_bpm.py (filtro 'solo sin validar')")


if __name__ == "__main__":
    main()
