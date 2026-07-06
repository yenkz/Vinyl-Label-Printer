"""
render_labels.py — STEP 4

Generates ONE image per vinyl record (not one per track), with the cover,
label, and release date in the header, and a table of all its tracks:
position, title, duration, BPM, and tonality (key, in Camelot notation: "8A").
The width is always 696px (the 62mm of the roll), and height varies based on
how many tracks the record has.

The cover is downloaded by fetch_discogs.py (step 1), and if Discogs didn't
have it, by Bandcamp/Spotify steps; if a record doesn't have one, the header
comes out text-only, like before.

BPM and key are in bold because that's what you'll be reading in the dark
in the booth.

Images are saved in the labels_output/ folder, with names like
"Artist - Record (id).png", ready for print_labels.py to send to the printer.

How to run it:
    python render_labels.py              # generate ALL labels
    python render_labels.py aphex        # only records containing "aphex"
    python render_labels.py aphex --ver  # also open them in Preview
"""

import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config
from comunes import a_camelot
from db import get_connection, init_db

OUTPUT_DIR = Path(__file__).parent / config.OUTPUT_DIR

# --- Label design measurements (in pixels, at 300dpi) ---
MARGIN = 16
HEADER_LINE = 44   # height of each header row (artist / record)
META_LINE = 32     # height of small rows (label / date)
TITLES_ROW = 30    # row with column names (DUR/BPM/KEY)
ROW_HEIGHT = 46
FOOTER_MARGIN = 16
COVER_PX = 170      # side of cover in header


def load_fonts():
    """Tries to load the configured font; if it doesn't exist on this
    computer, uses Pillow's default (less pretty, but it works)."""
    try:
        bold = ImageFont.truetype(config.FONT_PATH_BOLD, 34)
        text = ImageFont.truetype(config.FONT_PATH, 26)
        bpm_bold = ImageFont.truetype(config.FONT_PATH_BOLD, 26)
        meta = ImageFont.truetype(config.FONT_PATH, 22)
    except OSError:
        print("Warning: couldn't find configured font in FONT_PATH, using default.")
        bold = text = bpm_bold = meta = ImageFont.load_default()
    return bold, text, bpm_bold, meta


def format_date(release):
    """Vinyl release date, clean. Discogs sometimes sends "2005-00-00" when
    it only knows the year; we remove the "-00". If no date, use year only."""
    date = re.sub(r"(-00)+$", "", (release["released"] or "").strip())
    return date or (str(release["year"]) if release["year"] else "")


def load_cover(release):
    """Opens the cover downloaded by enrich_spotify.py and converts it to
    pure black and white with halftone — exactly how the thermal printer will
    print it, so what you see on screen is what comes out on paper."""
    if not release["cover_path"]:
        return None
    path = Path(__file__).parent / release["cover_path"]
    if not path.exists():
        return None
    try:
        tapa = Image.open(ruta).resize((COVER_PX, COVER_PX)).convert("L").convert("1")
    except OSError:
        return None
    return tapa.convert("RGB")


def truncate_text(draw, text, font, max_width):
    """Cuts text with '...' if it doesn't fit in available width."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "...", font=font) > max_width:
        text = text[:-1]
    return text + "..."


def file_name(release):
    """Creates a readable and valid file name, like
    "Aphex Twin - Selected Ambient Works (12345).png"."""
    base = f"{release['artist']} - {release['title']}"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return f"{base[:120]} ({release['release_id']}).png"


def renderizar_disco(release, tracks, fuente_titulo, fuente_texto, fuente_bpm, fuente_meta):
    tapa = cargar_tapa(release)
    sello = " · ".join(filter(None, (release["sello"], release["catno"])))
    fecha = formatear_fecha(release)

    # --- Encabezado: tapa a la izquierda (si hay) y a su lado el
    # artista (negrita), el disco, el sello y la fecha de edición ---
    texto_x = MARGIN + COVER_PX + 16 if tapa else MARGIN
    y_texto = 14 if tapa else 6
    y = y_texto + LINEA_HEADER * 2
    if sello:
        y += LINEA_META
    if fecha:
        y += LINEA_META
    header_height = max(y + 12, COVER_PX + 24 if tapa else 0)

    alto_total = header_height + FILA_TITULOS + ROW_HEIGHT * len(tracks) + FOOTER_MARGIN
    img = Image.new("RGB", (config.LABEL_WIDTH_PX, alto_total), "white")
    draw = ImageDraw.Draw(img)

    if tapa:
        img.paste(tapa, (MARGIN, 12))
    ancho_util = config.LABEL_WIDTH_PX - texto_x - MARGIN

    y = y_texto
    artista = truncar_texto(draw, release["artist"], fuente_titulo, ancho_util)
    draw.text((texto_x, y), artista, font=fuente_titulo, fill="black")
    y += LINEA_HEADER
    disco = truncar_texto(draw, release["title"], fuente_texto, ancho_util)
    draw.text((texto_x, y), disco, font=fuente_texto, fill="black")
    y += LINEA_HEADER
    if sello:
        draw.text((texto_x, y), truncar_texto(draw, sello, fuente_meta, ancho_util), font=fuente_meta, fill="black")
        y += LINEA_META
    if fecha:
        draw.text((texto_x, y), fecha, font=fuente_meta, fill="black")
    draw.line(
        [(MARGIN, header_height - 8), (config.LABEL_WIDTH_PX - MARGIN, header_height - 8)],
        fill="black",
        width=2,
    )

    # --- Columnas: posición | título del track | duración | BPM | key ---
    col_posicion_x = MARGIN
    col_titulo_x = MARGIN + 60
    col_duracion_x = config.LABEL_WIDTH_PX - 246
    col_bpm_x = config.LABEL_WIDTH_PX - 156
    col_key_x = config.LABEL_WIDTH_PX - 76

    # Nombres de las columnas, chiquitos, debajo de la línea. KEY solo
    # si algún track la tiene (en discos no electrónicos sería ruido).
    draw.text((col_duracion_x, header_height + 2), "DUR", font=fuente_meta, fill="black")
    draw.text((col_bpm_x, header_height + 2), "BPM", font=fuente_meta, fill="black")
    if any(t["key"] for t in tracks):
        draw.text((col_key_x, header_height + 2), "KEY", font=fuente_meta, fill="black")

    y = header_height + FILA_TITULOS
    for track in tracks:
        draw.text((col_posicion_x, y + 8), track["position"] or "", font=fuente_texto, fill="black")

        titulo_max_ancho = col_duracion_x - col_titulo_x - 10
        # En discos "Various" cada track puede tener su propio artista
        # (Discogs lo guarda por track); si lo tenemos, lo mostramos
        # antes del título para no perder esa info en la etiqueta.
        texto_track = track["title"] or ""
        if track["artist"]:
            texto_track = f"{track['artist']} – {texto_track}"
        titulo_track = truncar_texto(draw, texto_track, fuente_texto, titulo_max_ancho)
        draw.text((col_titulo_x, y + 8), titulo_track, font=fuente_texto, fill="black")

        draw.text((col_duracion_x, y + 8), track["duration_display"] or "--:--", font=fuente_texto, fill="black")

        if track["bpm"]:
            bpm_texto = str(round(track["bpm"]))
            # El asterisco marca los BPM dudosos (los detectores no se
            # pusieron de acuerdo y todavía no lo confirmaste).
            if track["bpm_needs_review"]:
                bpm_texto += "*"
        else:
            bpm_texto = "?"
        draw.text((col_bpm_x, y + 8), bpm_texto, font=fuente_bpm, fill="black")

        # La key en Camelot ("8A"). Sin "?" cuando falta: la mayoría
        # de los discos no electrónicos no la van a tener nunca.
        if track["key"]:
            draw.text((col_key_x, y + 8), a_camelot(track["key"]), font=fuente_bpm, fill="black")

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

    fuente_titulo, fuente_texto, fuente_bpm, fuente_meta = cargar_fuentes()

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

        img = renderizar_disco(release, tracks, fuente_titulo, fuente_texto, fuente_bpm, fuente_meta)

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
