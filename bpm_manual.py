"""
bpm_manual.py — PASO 7 alternativo (flujo por planilla CSV)

Sirve para completar a mano los BPM que la búsqueda automática no
encontró. Funciona en dos modos:

    python bpm_manual.py export
        -> Crea un archivo bpm_pendientes.csv con todos los tracks
           que todavía no tienen BPM. Abrilo con Excel/Numbers/Google
           Sheets, completá la columna "bpm" a mano (podés buscar el
           track en Shazam, Tunebat, o escuchándolo con un metrónomo),
           y guardalo como CSV de nuevo.

    python bpm_manual.py import
        -> Lee ese mismo archivo bpm_pendientes.csv y carga los BPM
           que hayas completado de vuelta en la base de datos.

Podés alternar export/completar/import las veces que quieras, a
medida que vas revisando discos.
"""

import csv
import sys
from pathlib import Path

from db import get_connection, init_db, registrar_bpm_fuente

CSV_PATH = Path(__file__).parent / "bpm_pendientes.csv"


def export_csv():
    init_db()  # por si todavía no corriste ningún otro paso
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT tracks.id, COALESCE(tracks.artist, releases.artist) AS artist,
               releases.title AS album,
               tracks.position, tracks.title AS track_title, tracks.bpm
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm IS NULL
        ORDER BY releases.artist, releases.title, tracks.position
        """
    )
    filas = cursor.fetchall()
    conn.close()

    if not filas:
        print("No hay tracks pendientes de BPM. ¡Ya está todo completo!")
        return

    # utf-8-sig: el "sig" hace que Excel abra bien los acentos y eñes.
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["track_id", "artist", "album", "position", "track_title", "bpm"])
        for row in filas:
            writer.writerow(
                [row["id"], row["artist"], row["album"], row["position"], row["track_title"], ""]
            )

    print(f"Exportado: {CSV_PATH}")
    print(f"{len(filas)} tracks pendientes. Completá la columna 'bpm' y después corré:")
    print("    python bpm_manual.py import")


def import_csv():
    if not CSV_PATH.exists():
        print(f"No encuentro {CSV_PATH}. Corré primero: python bpm_manual.py export")
        return

    init_db()  # por si la base viene de una versión anterior
    conn = get_connection()
    cursor = conn.cursor()

    actualizados = 0
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bpm_texto = (row.get("bpm") or "").strip()
            if not bpm_texto:
                continue  # todavía no lo completaste, lo dejamos para la próxima

            try:
                bpm = float(bpm_texto)
            except ValueError:
                print(f"   BPM inválido para track_id {row['track_id']}: '{bpm_texto}' (lo salteo)")
                continue

            # Cargarlo a mano ES la validación manual: queda con la ✓,
            # y anotado como fuente 'manual' junto a las demás.
            cursor.execute(
                "UPDATE tracks SET bpm = ?, bpm_source = 'manual', bpm_alt = NULL,"
                " bpm_needs_review = 0, bpm_verified = 1 WHERE id = ?",
                (bpm, row["track_id"]),
            )
            registrar_bpm_fuente(conn, int(row["track_id"]), "manual", bpm)
            actualizados += 1

    conn.commit()
    conn.close()

    print(f"Importados {actualizados} BPM cargados a mano.")


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else None

    if modo == "export":
        export_csv()
    elif modo == "import":
        import_csv()
    else:
        print(__doc__)
