"""
render_labels.py — PASO 4

Genera UNA imagen por cada vinilo (no una por track), con una tabla
de todos sus tracks: posición, título, duración y BPM. El ancho es
siempre 696px (los 62mm del rollo), y el alto varía según cuántos
tracks tenga el disco.

El BPM va en negrita porque es lo que vas a estar leyendo a oscuras
en la cabina.

Las imágenes quedan guardadas en la carpeta labels_output/, con
nombres tipo "Artista - Disco (id).png", listas para que
print_labels.py las mande a la impresora.

Cómo correrlo:
    python render_labels.py              # genera TODAS las etiquetas
    python render_labels.py aphex        # solo discos que contengan "aphex"
    python render_labels.py aphex --ver  # además las abre en Vista Previa
"""

import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config
from db import get_connection, init_db

OUTPUT_DIR = Path(__file__).parent / config.OUTPUT_DIR

# --- Medidas del diseño de la etiqueta (en píxeles, a 300dpi) ---
MARGIN = 16
LINEA_HEADER = 44   # alto de cada renglón del encabezado (artista / disco)
ROW_HEIGHT = 46
FOOTER_MARGIN = 16


def cargar_fuentes():
    """Intenta cargar la fuente configurada; si no existe en esta
    computadora, usa la básica de Pillow (menos linda, pero funciona)."""
    try:
        negrita = ImageFont.truetype(config.FONT_PATH_BOLD, 34)
        texto = ImageFont.truetype(config.FONT_PATH, 26)
        bpm_negrita = ImageFont.truetype(config.FONT_PATH_BOLD, 26)
    except OSError:
        print("Aviso: no encontré la fuente configurada en FONT_PATH, uso una básica.")
        negrita = texto = bpm_negrita = ImageFont.load_default()
    return negrita, texto, bpm_negrita


def truncar_texto(draw, texto, fuente, ancho_maximo):
    """Corta el texto con '...' si no entra en el ancho disponible."""
    if draw.textlength(texto, font=fuente) <= ancho_maximo:
        return texto
    while texto and draw.textlength(texto + "...", font=fuente) > ancho_maximo:
        texto = texto[:-1]
    return texto + "..."


def nombre_de_archivo(release):
    """Arma un nombre de archivo legible y válido, tipo
    "Aphex Twin - Selected Ambient Works (12345).png"."""
    base = f"{release['artist']} - {release['title']}"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return f"{base[:120]} ({release['release_id']}).png"


def renderizar_disco(release, tracks, fuente_titulo, fuente_texto, fuente_bpm):
    header_height = LINEA_HEADER * 2 + 14
    alto_total = header_height + ROW_HEIGHT * len(tracks) + FOOTER_MARGIN
    img = Image.new("RGB", (config.LABEL_WIDTH_PX, alto_total), "white")
    draw = ImageDraw.Draw(img)

    ancho_util = config.LABEL_WIDTH_PX - 2 * MARGIN

    # --- Encabezado en dos renglones: artista (negrita) y disco ---
    artista = truncar_texto(draw, release["artist"], fuente_titulo, ancho_util)
    draw.text((MARGIN, 6), artista, font=fuente_titulo, fill="black")
    disco = truncar_texto(draw, release["title"], fuente_texto, ancho_util)
    draw.text((MARGIN, 6 + LINEA_HEADER), disco, font=fuente_texto, fill="black")
    draw.line(
        [(MARGIN, header_height - 8), (config.LABEL_WIDTH_PX - MARGIN, header_height - 8)],
        fill="black",
        width=2,
    )

    # --- Columnas: posición | título del track | duración | BPM ---
    col_posicion_x = MARGIN
    col_titulo_x = MARGIN + 60
    col_duracion_x = config.LABEL_WIDTH_PX - 190
    col_bpm_x = config.LABEL_WIDTH_PX - 100

    y = header_height
    for track in tracks:
        draw.text((col_posicion_x, y + 8), track["position"] or "", font=fuente_texto, fill="black")

        titulo_max_ancho = col_duracion_x - col_titulo_x - 10
        titulo_track = truncar_texto(draw, track["title"] or "", fuente_texto, titulo_max_ancho)
        draw.text((col_titulo_x, y + 8), titulo_track, font=fuente_texto, fill="black")

        draw.text((col_duracion_x, y + 8), track["duration_display"] or "--:--", font=fuente_texto, fill="black")

        bpm_texto = str(round(track["bpm"])) if track["bpm"] else "?"
        draw.text((col_bpm_x, y + 8), bpm_texto, font=fuente_bpm, fill="black")

        y += ROW_HEIGHT

    return img


def main():
    argumentos = sys.argv[1:]
    abrir_preview = "--ver" in argumentos
    filtro = next((a.lower() for a in argumentos if not a.startswith("--")), "")

    OUTPUT_DIR.mkdir(exist_ok=True)

    init_db()  # por si todavía no corriste ningún otro paso
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()

    if not releases:
        print("Tu colección está vacía. Corré primero: python fetch_discogs.py")
        return

    if filtro:
        releases = [r for r in releases if filtro in f"{r['artist']} {r['title']}".lower()]
        if not releases:
            print(f"Ningún disco de tu colección contiene '{filtro}'.")
            return

    fuente_titulo, fuente_texto, fuente_bpm = cargar_fuentes()

    generados = 0
    rutas_generadas = []
    for release in releases:
        cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY id",
            (release["release_id"],),
        )
        tracks = cursor.fetchall()
        if not tracks:
            continue

        img = renderizar_disco(release, tracks, fuente_titulo, fuente_texto, fuente_bpm)

        nombre = nombre_de_archivo(release)
        img.save(OUTPUT_DIR / nombre)
        rutas_generadas.append(OUTPUT_DIR / nombre)
        generados += 1
        print(f"Generado: {nombre}")

    conn.close()
    print(f"\nListo. {generados} etiquetas generadas en {OUTPUT_DIR}/")

    if abrir_preview and rutas_generadas:
        # Abre las imágenes en Vista Previa (Mac) para chequearlas
        # antes de gastar etiqueta.
        subprocess.run(["open", *map(str, rutas_generadas)])

    print("Próximo paso: python print_labels.py --prueba  (para ver qué saldría)")
    print("           o: python print_labels.py           (para imprimir)")


if __name__ == "__main__":
    main()
