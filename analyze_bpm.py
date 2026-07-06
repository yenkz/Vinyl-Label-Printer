"""
analyze_bpm.py — PASO 5 (el fallback de Beatport)

Para cada track que sigue sin BPM (porque Beatport no lo tuvo), lo
busca en Bandcamp, YouTube o SoundCloud (en ese orden), baja el audio
a una carpeta temporal, mide el tempo localmente y guarda el
resultado, anotado como fuente "youtube" en bpm_sources (con el video
del que salió). El audio se borra apenas se analiza.

Bandcamp se prueba primero: para sellos chicos de música electrónica
suele tener el audio original (no un repost), así que cuando está,
es la fuente más confiable. yt-dlp no tiene un modo "búsqueda" para
Bandcamp como sí tiene para YouTube/SoundCloud, así que primero se
usa la API de autocompletado de bandcamp.com para encontrar la URL
del track.

Cada buscador se consulta con cada artista del crédito ("B.Love /
Jhobei" son dos búsquedas) y también con el número de catálogo del
vinilo ("SEMID026 ..."): los sellos chicos suelen titular sus
subidas por catálogo y no por artista.

Para no medir cualquier video, compara la duración del video con la
duración que figura en Discogs: si no coinciden razonablemente, lo
descarta. Los tracks sin duración en Discogs se aceptan igual, pero
solo si el video dura entre 2 y 15 minutos.

Si ningún candidato pasa el filtro de duración pero alguno clava
título y artista, se lo mide igual como último recurso: una duración
distinta casi siempre es otra edición del mismo tema (la versión de
álbum vs. la del 12", o una duración mal cargada en Discogs), y el
tempo no cambia entre ediciones. El resultado de ese rescate queda
SIEMPRE marcado como dudoso, con las dos duraciones anotadas en la
fuente, para que decidas vos en el editor. Y los videos con pinta de
"EP entero / preview del sello" quedan afuera del rescate: un
minimix tiene varios tempos y mediría cualquier cosa.

El tempo se mide con DOS detectores distintos (deeprhythm, una red
neuronal entrenada con música electrónica, y librosa, el clásico).
Si los dos coinciden, el número es confiable — pero igual NADA queda
validado solo: la ✓ verde la ponés vos en el editor (python
edit_bpm.py), viendo todas las fuentes. Si no coinciden — típico
error de "un detector escuchó 89 donde el otro escucha 134" — se
guarda el de deeprhythm igual, pero el track queda marcado como
dudoso, con el otro candidato a un click de distancia en el editor.

Como los detectores de tempo a veces devuelven el doble o la mitad,
el resultado se acomoda al rango típico de música de club (88–176).
Si tu colección es de otro palo (hip hop, ambient...), ajustá
BPM_MINIMO / BPM_MAXIMO en comunes.py.

Cómo correrlo:
    python analyze_bpm.py        # analiza todos los que faltan
    python analyze_bpm.py 5      # solo 5 (para probar)

Se puede cortar con Ctrl+C cuando quieras: lo ya analizado queda
guardado, y la próxima vez sigue desde donde quedó.
"""

import difflib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
import librosa
import numpy as np
import requests
from yt_dlp import YoutubeDL

import config
from comunes import acomodar_al_rango, formatear_duracion, parsear_duracion
from db import get_connection, init_db, registrar_bpm_fuente

# Si los dos detectores difieren en más que esto (ya acomodados al
# rango de comunes.py), el track queda marcado para revisar a mano.
TOLERANCIA_BPM = 2.5

# Cuánto puede diferir el video de la duración de Discogs para
# considerarlo el track correcto: 20 segundos o 12%, lo que sea mayor.
TOLERANCIA_SEG = 20
TOLERANCIA_PORCENTAJE = 0.12

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# API pública (sin key) que usa el buscador de bandcamp.com.
BANDCAMP_SEARCH_API = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

# Dónde buscar el audio, en orden, después de probar Bandcamp. Si
# YouTube se pone en modo anti-bot o no tiene el track, se prueba
# SoundCloud (donde vive buena parte de la música de sellos chicos).
BUSCADORES = [
    ("YouTube", "ytsearch6"),
    ("SoundCloud", "scsearch6"),
]


def opciones_base():
    ops = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if config.YOUTUBE_COOKIES_NAVEGADOR:
        ops["cookiesfrombrowser"] = (config.YOUTUBE_COOKIES_NAVEGADOR,)
    return ops


def resumir_error(e):
    """Deja el error de yt-dlp en una línea entendible."""
    texto = str(e).split("\n")[0]
    if "Sign in to confirm" in texto:
        return "YouTube pide login (modo anti-bot; suele pasarse solo en unas horas)"
    if "DRM protected" in texto:
        return "lo sirve con DRM (no se puede bajar)"
    return texto[:120]


# Palabras que no dicen nada sobre QUÉ track es (aparecen en
# cualquier título) y por eso no cuentan para comparar.
PALABRAS_VACIAS = {"the", "and", "you", "your", "feat", "with", "mix", "original"}


def palabras(texto):
    """Pasa un título a un conjunto de palabras comparables."""
    limpio = "".join(c.lower() if c.isalnum() else " " for c in texto)
    return {p for p in limpio.split() if len(p) > 2 and p not in PALABRAS_VACIAS}


def separar_artistas(artista):
    """Devuelve la lista de artistas de un crédito compuesto de
    Discogs ("B.Love / Jhobei" -> ["B.Love", "Jhobei"]). En un split
    o una colaboración el track puede estar publicado bajo cualquiera
    de los nombres, así que hay que buscar con cada uno y dar por
    bueno un video de cualquiera de los dos. Para "Various" o
    "Desconocido" devuelve lista vacía (no hay artista que chequear)."""
    if artista.lower() in ("various", "desconocido"):
        return []
    return [parte.strip() for parte in artista.split(" / ") if parte.strip()]


def tokens_de_artistas(artista):
    """Palabras comparables de TODOS los artistas del crédito, para
    el chequeo de "¿el artista aparece en el video?"."""
    tokens = set()
    for parte in separar_artistas(artista):
        tokens |= palabras(parte)
    return tokens


def armar_consultas(artista, titulo, catno):
    """Las búsquedas a probar para un track: una por cada artista del
    crédito y, si el vinilo tiene número de catálogo, una más con él
    ("SEMID026 R U Listening..."): los sellos chicos suelen titular
    sus subidas por catálogo y no por artista."""
    consultas = [f"{parte} {titulo}" for parte in separar_artistas(artista)] or [titulo]
    if catno:
        consultas.append(f"{catno} {titulo}")
    return consultas


# Señales de que el video no es UN track sino el EP entero o un
# preview del sello ("R U Listening EP inc Sweely Remix", 8:34).
# Pasan el chequeo de título y artista y solo la duración los delata;
# por eso el rescate (que afloja la duración) los excluye: un minimix
# mezcla varios tempos y mediría cualquier cosa.
PALABRAS_COMPILADO = {"ep", "lp", "va", "inc", "incl", "minimix", "megamix",
                      "preview", "previews", "snippet", "snippets",
                      "sampler", "showreel", "teaser"}


def parece_compilado(titulo_video):
    limpio = "".join(c.lower() if c.isalnum() else " " for c in titulo_video)
    return bool(set(limpio.split()) & PALABRAS_COMPILADO)


# Umbral de la comparación letra por letra (ver similitud_parcial):
# a partir de acá lo tratamos como "mismo título".
UMBRAL_FUZZY = 0.75


def compacto(texto):
    """Deja solo letras y números en minúscula, sin espacios ni
    puntuación — para comparar "Snap-Shot" con "Snapshot" o "Sugar
    Coated" con "Sugarcoated" como si fueran el mismo texto."""
    return "".join(c.lower() for c in texto if c.isalnum())


def similitud_parcial(a, b):
    """Qué tan bien encaja el más corto de los dos textos DENTRO del
    más largo, letra por letra. A diferencia de comparar las dos
    cadenas de punta a punta, esto no castiga que el candidato traiga
    decoración de más (nombre de sello, artista, etc.)."""
    a, b = compacto(a), compacto(b)
    if not a or not b:
        return 0.0
    corto, largo = (a, b) if len(a) <= len(b) else (b, a)
    mejor = 0.0
    for i in range(len(largo) - len(corto) + 1):
        mejor = max(mejor, difflib.SequenceMatcher(None, corto, largo[i:i + len(corto)]).ratio())
        if mejor == 1.0:
            break
    return mejor


def titulo_coincide(titulo_track, titulo_candidato):
    """True si el candidato parece ser el mismo tema: comparte la
    mitad de las palabras con el de Discogs (chequeo normal), o —
    cuando lo escriben distinto, con guion, sin espacio, o con una
    letra de más o de menos, como pasa seguido en Bandcamp— el texto
    es lo bastante parecido letra por letra."""
    objetivo = palabras(titulo_track)
    del_candidato = palabras(titulo_candidato)
    if objetivo and len(objetivo & del_candidato) / len(objetivo) >= 0.5:
        return True
    return similitud_parcial(titulo_track, titulo_candidato) >= UMBRAL_FUZZY


def elegir_videos(candidatos, artista, titulo_track, duracion_objetivo):
    """Separa los resultados de la búsqueda en dos listas y devuelve
    (aprobados, rescate), las dos ordenadas del que mejor pega en
    duración al peor. Los aprobados pasan los tres chequeos; los de
    rescate pegan en título y artista pero NO en duración, y sirven
    como último recurso (ver el rescate en analizar_track). Son
    listas y no un solo video para que, si la descarga del mejor
    falla — SoundCloud sirve algunos tracks con DRM —, se pueda
    probar el siguiente.

    Tres chequeos, porque cada uno solo se equivoca:
      - el título del video tiene que ser (o parecerse mucho a) el
        título del track (si no, en un EP el buscador te da otro
        tema del mismo artista que dura parecido),
      - el artista tiene que aparecer en el título del video o en el
        canal que lo subió (si no, "Free The Drums" matchea con
        cualquier video que diga "FREE DOWNLOAD ... drums"), y
      - la duración tiene que coincidir con la de Discogs (si la hay);
        si no, entre "Tema X" y "Tema X (Remix)" agarra cualquiera.
    """
    tokens_artista = tokens_de_artistas(artista)

    aprobados = []
    rescate = []
    for video in candidatos:
        dur = video.get("duration")
        if not dur:
            continue

        titulo_video = video.get("title", "")
        if not titulo_coincide(titulo_track, titulo_video):
            continue

        canal = video.get("uploader") or video.get("channel") or ""
        if tokens_artista and not tokens_artista & (palabras(titulo_video) | palabras(canal)):
            continue

        if duracion_objetivo:
            tolerancia = max(TOLERANCIA_SEG, duracion_objetivo * TOLERANCIA_PORCENTAJE)
            diferencia = abs(dur - duracion_objetivo)
            if diferencia <= tolerancia:
                aprobados.append((diferencia, video))
            elif 120 <= dur <= 900 and not parece_compilado(titulo_video):
                rescate.append((diferencia, video))
        elif 120 <= dur <= 900:
            aprobados.append((0, video))
    aprobados.sort(key=lambda par: par[0])
    rescate.sort(key=lambda par: par[0])
    return [v for _, v in aprobados], [v for _, v in rescate]


def buscar_bandcamp(artista, titulo, duracion_objetivo, catno=None):
    """Busca el track en Bandcamp y devuelve (aprobados, rescate)
    como elegir_videos, con dicts {title, url, duration, uploader}.

    Bandcamp no tiene un modo "búsqueda" en yt-dlp, así que primero
    le preguntamos a la API de autocompletado de bandcamp.com (la
    misma que usa la lupa del sitio) por candidatos, filtramos por
    título/artista igual que en elegir_videos, y recién a los que
    matchean por texto les pedimos a yt-dlp la duración real (la
    búsqueda de bandcamp.com no la incluye) para confirmar que es el
    track correcto antes de bajar el audio.
    """
    consultas = armar_consultas(artista, titulo, catno)
    tokens_artista = tokens_de_artistas(artista)

    candidatos = []
    vistos = set()
    for consulta in consultas:
        try:
            resp = requests.post(
                BANDCAMP_SEARCH_API,
                json={
                    "search_text": consulta,
                    "search_filter": "track",
                    "full_page": False,
                    "fan_id": None,
                },
                timeout=10,
            )
            resp.raise_for_status()
            resultados = resp.json().get("auto", {}).get("results") or []
        except (requests.RequestException, ValueError):
            continue

        for r in resultados:
            if r.get("type") != "t" or not r.get("item_url_path"):
                continue
            if r["item_url_path"] in vistos:
                continue
            vistos.add(r["item_url_path"])
            if not titulo_coincide(titulo, r.get("name", "")):
                continue
            if tokens_artista and not tokens_artista & palabras(r.get("band_name", "")):
                continue
            candidatos.append(r)

    rescate = []
    for candidato in candidatos[:3]:
        url = candidato["item_url_path"]
        try:
            with YoutubeDL(opciones_base()) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            continue

        dur = info.get("duration")
        if not dur:
            continue
        video = {
            "title": info.get("title") or f"{candidato.get('band_name', '')} - {candidato.get('name', '')}",
            "url": url,
            "duration": dur,
            "uploader": candidato.get("band_name", ""),
        }
        if duracion_objetivo:
            tolerancia = max(TOLERANCIA_SEG, duracion_objetivo * TOLERANCIA_PORCENTAJE)
            if abs(dur - duracion_objetivo) <= tolerancia:
                return [video], rescate  # con uno que pasa todo alcanza
            if 120 <= dur <= 900 and not parece_compilado(video["title"]):
                rescate.append(video)
        elif 120 <= dur <= 900:
            return [video], rescate
    return [], rescate


# El modelo de deeprhythm tarda unos segundos en cargar (y la primera
# vez baja sus pesos de internet), así que lo cargamos una sola vez,
# recién cuando hace falta.
_modelo_deeprhythm = None


def modelo_deeprhythm():
    global _modelo_deeprhythm
    if _modelo_deeprhythm is None:
        from deeprhythm import DeepRhythmPredictor
        _modelo_deeprhythm = DeepRhythmPredictor()
    return _modelo_deeprhythm


def medir_bpm(ruta_audio, duracion_video):
    """Recorta un pedazo del medio del track (donde ya entró el beat),
    lo convierte a WAV y mide el tempo con los dos detectores.

    Devuelve (bpm, alternativa, dudoso):
      - detectores de acuerdo:    (bpm, None, False) — número confiable
        (igual lo validás vos en edit_bpm.py, nada se valida solo),
      - detectores en desacuerdo: (deeprhythm, librosa, True) —
        se guarda el primero, marcado para confirmar en edit_bpm.py,
      - midió uno solo:           (ese bpm, None, True si fue librosa),
      - no se pudo medir nada:    (None, None, False).

    ¿Por qué dos detectores? Porque librosa solo a veces se engancha
    a un pulso que no es (mide 89 en un track de 134: un error de
    2/3 que ningún ajuste de rango puede corregir). deeprhythm es
    mucho más preciso en música electrónica, y la coincidencia entre
    ambos es lo que nos dice si el número es de fiar.
    """
    wav = ruta_audio.with_suffix(".wav")
    inicio = min(60, int(duracion_video // 3)) if duracion_video else 30
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-ss", str(inicio), "-i", str(ruta_audio),
         "-t", "60", "-ac", "1", "-ar", "22050", str(wav)],
        check=True,
    )

    # Detector 1: deeprhythm (ojo: necesita el WAV, no lee webm/m4a).
    try:
        bpm_dr = acomodar_al_rango(float(modelo_deeprhythm().predict(str(wav))))
    except Exception:
        bpm_dr = None

    # Detector 2: librosa.
    y, sr = librosa.load(str(wav), sr=None, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm_lr = acomodar_al_rango(float(np.atleast_1d(tempo)[0]))

    if bpm_dr is None and bpm_lr is None:
        return None, None, False
    if bpm_lr is None:
        return bpm_dr, None, False   # deeprhythm solo: confiable
    if bpm_dr is None:
        return bpm_lr, None, True    # librosa solo: mejor confirmarlo
    if abs(bpm_dr - bpm_lr) <= TOLERANCIA_BPM:
        return bpm_dr, None, False   # dos detectores de acuerdo
    return bpm_dr, bpm_lr, True


def bajar_y_medir(video, tmpdir):
    """Baja el audio del video a tmpdir, mide el tempo y borra el
    archivo. Devuelve lo mismo que medir_bpm. Si la descarga falla
    (p. ej. SoundCloud sirviendo el track con DRM), deja subir la
    excepción para que el que llama pruebe otro candidato."""
    ops_descarga = opciones_base()
    ops_descarga.update(
        {
            "noprogress": True,
            "format": "bestaudio/best",
            "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        }
    )
    try:
        with YoutubeDL(ops_descarga) as ydl:
            info = ydl.extract_info(video["url"], download=True)
            ruta_audio = Path(ydl.prepare_filename(info))
        return medir_bpm(ruta_audio, video.get("duration"))
    finally:
        # borramos el audio apenas lo medimos (o lo que haya quedado
        # de una descarga fallida)
        for archivo in Path(tmpdir).iterdir():
            archivo.unlink()


def analizar_track(artista, titulo, duracion_objetivo, tmpdir, catno=None):
    """Busca el track (Bandcamp primero, YouTube y SoundCloud si no),
    baja el mejor candidato y devuelve (bpm, alternativa, dudoso,
    detalle). Si ningún candidato pasa el filtro de duración pero
    alguno clava título y artista, lo mide igual como último recurso
    (ver la pasada de rescate abajo). Si no se pudo nada, bpm viene
    None y detalle explica el motivo."""
    consultas = armar_consultas(artista, titulo, catno)

    motivos = []
    rescates = []  # (buscador, video) que pegan en todo menos en duración
    for nombre, prefijo in [("Bandcamp", None)] + BUSCADORES:
        try:
            if prefijo is None:
                videos, rescate = buscar_bandcamp(artista, titulo, duracion_objetivo, catno)
            else:
                ops_busqueda = opciones_base()
                ops_busqueda["extract_flat"] = "in_playlist"
                candidatos = []
                vistos = set()
                with YoutubeDL(ops_busqueda) as ydl:
                    for consulta in consultas:
                        busqueda = ydl.extract_info(f"{prefijo}:{consulta}", download=False)
                        for entrada in busqueda.get("entries") or []:
                            clave = entrada.get("url") or entrada.get("id")
                            if clave in vistos:
                                continue
                            vistos.add(clave)
                            candidatos.append(entrada)
                videos, rescate = elegir_videos(candidatos, artista, titulo, duracion_objetivo)
        except Exception as e:
            motivos.append(f"{nombre}: {resumir_error(e)}")
            continue

        rescates.extend((nombre, video) for video in rescate)
        if not videos:
            motivos.append(f"{nombre}: sin resultado que coincida en artista, título y duración")
            continue

        # Si la descarga del mejor candidato falla (típico: SoundCloud
        # lo sirve con DRM), probamos los siguientes: muchas veces hay
        # otra subida del mismo tema que sí se puede bajar.
        for video in videos[:3]:
            try:
                bpm, alternativa, dudoso = bajar_y_medir(video, tmpdir)
            except Exception as e:
                motivos.append(f"{nombre}: {resumir_error(e)}")
                continue
            if bpm is None:
                motivos.append(f"{nombre}: no pude medir un tempo claro")
                continue
            return bpm, alternativa, dudoso, f"{video.get('title', '')} [{nombre}]"

    # Pasada de rescate: nadie pasó el filtro completo, pero estos
    # candidatos pegan en título y artista y solo fallan en duración.
    # Casi siempre es otra edición del mismo tema (la versión de álbum
    # vs. la del 12", o una duración mal cargada en Discogs), y el
    # tempo no cambia entre ediciones. Eso sí: el resultado queda
    # SIEMPRE dudoso, con las dos duraciones anotadas, para que la
    # última palabra la tengas vos en el editor.
    rescates.sort(key=lambda par: abs(par[1]["duration"] - duracion_objetivo))
    for nombre, video in rescates[:2]:
        try:
            bpm, alternativa, _ = bajar_y_medir(video, tmpdir)
        except Exception as e:
            motivos.append(f"{nombre}: {resumir_error(e)}")
            continue
        if bpm is None:
            motivos.append(f"{nombre}: no pude medir un tempo claro")
            continue
        detalle = (f"{video.get('title', '')} [{nombre}; ojo: dura "
                   f"{formatear_duracion(video['duration'])} y en Discogs figura "
                   f"{formatear_duracion(duracion_objetivo)} — ¿otra edición?]")
        return bpm, alternativa, True, detalle

    return None, None, False, " | ".join(motivos)


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
        SELECT tracks.id, tracks.title, tracks.duration_display,
               COALESCE(tracks.artist, releases.artist) AS artist,
               releases.catno
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
    dudosos = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, row in enumerate(pendientes, start=1):
            etiqueta = f"[{i}/{len(pendientes)}] {row['artist']} - {row['title']}"
            try:
                bpm, alternativa, dudoso, detalle = analizar_track(
                    row["artist"], row["title"],
                    parsear_duracion(row["duration_display"]), tmpdir,
                    row["catno"],
                )
            except KeyboardInterrupt:
                print("\nCortado. Lo analizado hasta acá quedó guardado.")
                break
            except Exception as e:
                print(f"{etiqueta}\n   -> error, sigo con el resto: {e}")
                continue

            if bpm:
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'youtube',"
                    " bpm_alt = ?, bpm_needs_review = ?, bpm_verified = 0 WHERE id = ?",
                    (bpm, alternativa, int(dudoso), row["id"]),
                )
                registrar_bpm_fuente(conn, row["id"], "youtube", bpm, detalle)
                conn.commit()
                encontrados += 1
                dudosos += int(dudoso)
                if dudoso and alternativa:
                    aviso = f"  [DUDOSO: el otro detector midió {alternativa:g}]"
                elif dudoso:
                    aviso = "  [DUDOSO: midió un solo detector]"
                else:
                    aviso = ""
                print(f"{etiqueta} -> {bpm:g} BPM{aviso}\n   (medido de: {detalle})")
            else:
                print(f"{etiqueta}\n   -> {detalle}")

            # Pausa entre tracks para no despertar al anti-bot de YouTube.
            time.sleep(3)

    conn.close()
    print("\n" + "=" * 50)
    print(f"BPM medido para {encontrados} de {len(pendientes)} tracks.")
    if dudosos:
        print(f"{dudosos} quedaron marcados como dudosos (los detectores no")
        print("coincidieron), con el otro candidato a un click en el editor.")
    else:
        print("Los dos detectores coincidieron en todos: buena señal.")
    print("Falta tu parte: validalos en el editor: python edit_bpm.py")


if __name__ == "__main__":
    main()
