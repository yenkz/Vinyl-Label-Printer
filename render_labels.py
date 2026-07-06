"""
render_labels.py — PASO 4

Genera UNA imagen por cada vinilo (no una por track), con la tapa
del disco, el sello y la fecha de edición en el encabezado, y una
tabla de todos sus tracks: posición, título, duración, BPM y
tonalidad (key, en notación Camelot: "8A"). El ancho es siempre
696px (los 62mm del rollo), y el alto varía según cuántos tracks
tenga el disco.

La tapa la baja fetch_discogs.py (paso 1), y si Discogs no la tuvo,
los pasos de Bandcamp/Spotify; si un disco no la tiene, el
encabezado sale solo con texto, como antes.

El BPM y la key van en negrita porque son lo que vas a estar
leyendo a oscuras en la cabina.

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
from comunes import a_camelot
from db import get_connection, init_db

OUTPUT_DIR = Path(__file__).parent / config.OUTPUT_DIR

# --- Medidas del diseño de la etiqueta (en píxeles, a 300dpi) ---
MARGIN = 16
LINEA_HEADER = 44   # alto de cada renglón del encabezado (artista / disco)
LINEA_META = 32     # alto de los renglones chicos (sello / fecha)
FILA_TITULOS = 30   # renglón con los nombres de las columnas (DUR/BPM/KEY)
ROW_HEIGHT = 46
FOOTER_MARGIN = 16
COVER_PX = 170      # lado de la tapa en el encabezado


def cargar_fuentes():
    """Intenta cargar la fuente configurada; si no existe en esta
    computadora, usa la básica de Pillow (menos linda, pero funciona)."""
    try:
        negrita = ImageFont.truetype(config.FONT_PATH_BOLD, 34)
        texto = ImageFont.truetype(config.FONT_PATH, 26)
        bpm_negrita = ImageFont.truetype(config.FONT_PATH_BOLD, 26)
        meta = ImageFont.truetype(config.FONT_PATH, 22)
    except OSError:
        print("Aviso: no encontré la fuente configurada en FONT_PATH, uso una básica.")
        negrita = texto = bpm_negrita = meta = ImageFont.load_default()
    return negrita, texto, bpm_negrita, meta


def formatear_fecha(release):
    """Fecha de edición del vinilo, limpia. Discogs a veces manda
    "2005-00-00" cuando solo sabe el año; le sacamos los "-00". Si no
    hay fecha, usamos el año solo."""
    fecha = re.sub(r"(-00)+$", "", (release["released"] or "").strip())
    return fecha or (str(release["year"]) if release["year"] else "")


def cargar_tapa(release):
    """Abre la tapa que bajó enrich_spotify.py y la pasa a blanco y
    negro puro con tramado — exactamente como la va a imprimir la
    térmica, así lo que ves en pantalla es lo que sale en papel."""
    if not release["cover_path"]:
        return None
    ruta = Path(__file__).parent / release["cover_path"]
    if not ruta.exists():
        return None
    try:
        tapa = Image.open(ruta).resize((COVER_PX, COVER_PX)).convert("L").convert("1")
    except OSError:
        return None
    return tapa.convert("RGB")


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
