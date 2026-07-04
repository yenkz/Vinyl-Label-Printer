"""
analyze_bpm.py — PASO 2b (opcional, para lo que Deezer no tiene)

Para cada track que sigue sin BPM, lo busca en YouTube, baja el audio
a una carpeta temporal, mide el tempo localmente (con librosa) y
guarda el resultado. El audio se borra apenas se analiza.

Para no medir cualquier video, compara la duración del video con la
duración que figura en Discogs: si no coinciden razonablemente, lo
descarta. Los tracks sin duración en Discogs se aceptan igual, pero
solo si el video dura entre 2 y 15 minutos.

Como los detectores de tempo a veces devuelven el doble o la mitad,
el resultado se acomoda al rango típico de música de club (88–176).
Si tu colección es de otro palo (hip hop, ambient...), ajustá
BPM_MINIMO / BPM_MAXIMO acá abajo.

Cómo correrlo:
    python analyze_bpm.py        # analiza todos los que faltan
    python analyze_bpm.py 5      # solo 5 (para probar)

Se puede cortar con Ctrl+C cuando quieras: lo ya analizado queda
guardado, y la próxima vez sigue desde donde quedó.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
import librosa
import numpy as np
from yt_dlp import YoutubeDL

from db import get_connection, init_db

# Rango de BPM esperable en tu colección: si la medición cae afuera,
# se dobla o se parte a la mitad hasta entrar (corrige los típicos
# errores de "mitad de tempo" de los detectores).
BPM_MINIMO = 88
BPM_MAXIMO = 176

# Cuánto puede diferir el video de la duración de Discogs para
# considerarlo el track correcto: 20 segundos o 12%, lo que sea mayor.
TOLERANCIA_SEG = 20
TOLERANCIA_PORCENTAJE = 0.12

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

OPCIONES_BUSQUEDA = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "noplaylist": True,
}


def parsear_duracion(texto):
    """Convierte "6:30" (o "1:02:15") a segundos. Devuelve None si
    no hay duración cargada en Discogs."""
    if not texto or ":" not in texto:
        return None
    try:
        partes = [int(p) for p in texto.split(":")]
    except ValueError:
        return None
    segundos = 0
    for p in partes:
        segundos = segundos * 60 + p
    return segundos or None


def elegir_video(candidatos, duracion_objetivo):
    """Elige el video cuya duración mejor coincida con la de Discogs.
    Sin duración de referencia, agarra el primero con un largo
    razonable para un track (ni un corte de 30s ni un mix de 1 hora)."""
    mejores = []
    for video in candidatos:
        dur = video.get("duration")
        if not dur:
            continue
        if duracion_objetivo:
            tolerancia = max(TOLERANCIA_SEG, duracion_objetivo * TOLERANCIA_PORCENTAJE)
            diferencia = abs(dur - duracion_objetivo)
            if diferencia <= tolerancia:
                mejores.append((diferencia, video))
        elif 120 <= dur <= 900:
            mejores.append((0, video))
            break
    if not mejores:
        return None
    return min(mejores, key=lambda par: par[0])[1]


def medir_bpm(ruta_audio, duracion_video):
    """Recorta un pedazo del medio del track (donde ya entró el beat),
    lo convierte a WAV y mide el tempo con librosa."""
    wav = ruta_audio.with_suffix(".wav")
    inicio = min(60, int(duracion_video // 3)) if duracion_video else 30
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-ss", str(inicio), "-i", str(ruta_audio),
         "-t", "60", "-ac", "1", "-ar", "22050", str(wav)],
        check=True,
    )

    y, sr = librosa.load(str(wav), sr=None, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    if not bpm:
        return None

    while bpm < BPM_MINIMO:
        bpm *= 2
    while bpm > BPM_MAXIMO:
        bpm /= 2
    return round(bpm, 1)


def analizar_track(artista, titulo, duracion_objetivo, tmpdir):
    """Busca el track en YouTube, baja el mejor candidato y devuelve
    (bpm, titulo_del_video) o (None, motivo)."""
    if artista.lower() in ("various", "desconocido"):
        consulta = titulo
    else:
        consulta = f"{artista.split(' / ')[0]} {titulo}"

    with YoutubeDL(OPCIONES_BUSQUEDA) as ydl:
        busqueda = ydl.extract_info(f"ytsearch6:{consulta}", download=False)
    video = elegir_video(busqueda.get("entries") or [], duracion_objetivo)
    if video is None:
        return None, "sin video con duración que coincida"

    opciones_descarga = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": "bestaudio/best",
        "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        "noplaylist": True,
    }
    with YoutubeDL(opciones_descarga) as ydl:
        info = ydl.extract_info(video["url"], download=True)
        ruta_audio = Path(ydl.prepare_filename(info))

    try:
        bpm = medir_bpm(ruta_audio, video.get("duration"))
    finally:
        # borramos el audio apenas lo medimos
        for archivo in Path(tmpdir).iterdir():
            archivo.unlink()

    if bpm is None:
        return None, "no pude medir un tempo claro"
    return bpm, video.get("title", "")


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
    cursor.execute(
        """
        SELECT tracks.id, tracks.title, tracks.duration_display, releases.artist
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.bpm IS NULL
        ORDER BY releases.artist, releases.title, tracks.id
        """
    )
    pendientes = cursor.fetchall()
    if limite:
        pendientes = pendientes[:limite]

    print(f"Tracks a analizar: {len(pendientes)}")
    print("(esto baja el audio de YouTube y lo mide acá; tarda ~30s por track,")
    print(" podés cortar con Ctrl+C y retomar después)\n")

    encontrados = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pendientes, start=1):
            etiqueta = f"[{i}/{len(pendientes)}] {row['artist']} - {row['title']}"
            try:
                bpm, detalle = analizar_track(
                    row["artist"], row["title"],
                    parsear_duracion(row["duration_display"]), tmpdir,
                )
            except KeyboardInterrupt:
                print("\nCortado. Lo analizado hasta acá quedó guardado.")
                break
            except Exception as e:
                print(f"{etiqueta}\n   -> error, sigo con el resto: {e}")
                continue

            if bpm:
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'youtube' WHERE id = ?",
                    (bpm, row["id"]),
                )
                conn.commit()
                encontrados += 1
                print(f"{etiqueta} -> {bpm:g} BPM\n   (medido de: {detalle})")
            else:
                print(f"{etiqueta}\n   -> {detalle}")

            time.sleep(1)

    conn.close()
    print("\n" + "=" * 50)
    print(f"BPM medido para {encontrados} de {len(pendientes)} tracks.")
    print("Ojo: son mediciones automáticas — si alguno te suena raro,")
    print("corregilo con: python bpm_manual.py export / import")


if __name__ == "__main__":
    main()
