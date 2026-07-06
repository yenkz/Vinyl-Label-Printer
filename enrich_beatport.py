"""
enrich_beatport.py — PASO 2

Busca TODOS los tracks en Beatport, LA referencia de metadatos de
música electrónica (no solo los que están sin BPM: aunque un track ya
tenga uno medido, el dato oficial de Beatport se consulta igual, sí o
sí), y guarda lo que encuentra:

  - El BPM oficial (el de la ficha del track, cargado por el sello).
    Queda anotado como fuente en bpm_sources, y pasa a ser el BPM
    principal del track — salvo que vos ya hayas cargado o validado
    uno a mano, que siempre gana.
  - La tonalidad (key), que en la etiqueta sale en Camelot ("8A").
  - El ISRC, si todavía no estaba.

Además cruza fuentes: si un track ya tenía BPM automático (de la
medición de audio o de Deezer) y Beatport dice lo mismo, la duda
queda resuelta — pero NADA se valida solo: la ✓ verde la ponés
únicamente vos en el editor (python edit_bpm.py), donde ves todas
las fuentes lado a lado. Si dicen distinto, el track queda marcado
como dudoso, con el otro valor a un click.

¿Cómo entra sin API key? Beatport no da acceso público a su API,
pero su propio reproductor embebido (embed.beatport.com) usa un
"cliente anónimo" cuyas credenciales son públicas: viajan en el
JavaScript del reproductor a cualquier navegador. Este script hace
lo mismo que el reproductor: pide un token anónimo con esas
credenciales y consulta la API oficial (api.beatport.com/v4). Si
Beatport rota las credenciales, se vuelven a sacar solas del
JavaScript del embed.

Para no traer datos de otro tema, el candidato tiene que coincidir
en artista, en título (incluyendo el nombre del mix/remix) y en
duración con lo que dice Discogs.

Cómo correrlo:
    python enrich_beatport.py        # todos los tracks pendientes
    python enrich_beatport.py 5      # solo 5 (para probar)
"""

import re
import sys
import time

import requests

from comunes import (
    a_camelot,
    acomodar_al_rango,
    normalizar,
    normalizar_key,
    parsear_duracion,
    se_parecen,
)
from db import get_connection, init_db, registrar_bpm_fuente

BEATPORT_API = "https://api.beatport.com/v4"
BEATPORT_EMBED = "https://embed.beatport.com/"
BEATPORT_TOKEN_URL = "https://account.beatport.com/o/token/"

# Credenciales del cliente anónimo del reproductor embebido. Son
# públicas por diseño (cualquier navegador las recibe al abrir un
# embed de Beatport) y solo dan acceso de lectura anónima al catálogo.
# Si dejan de andar, credenciales_posibles() saca las nuevas del
# JavaScript del reproductor.
CLIENT_ID = "2tiTbKxmQFwnbFjMONU4k7njMRZmV3ZMwRBndiZs"
CLIENT_SECRET = (
    "RDUJyAk4zFEGtQ8rsTmylDSfxmALRNBn3D1BsRr7MKi3oa1TL9Mq9QxqUPK7loiu"
    "mXolEWbJcWa4IGAhtwnTz1cSXClGJ1tkkNCNWwRwjxIKTZJKOJxbwaNt0Rm3WG0v"
)

NAVEGADOR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Cuánto puede diferir la duración de Beatport de la de Discogs para
# dar el track por correcto: 15 segundos o un 8%, lo que sea mayor
# (las duraciones impresas en las fundas suelen estar redondeadas).
TOLERANCIA_SEG = 15
TOLERANCIA_PORCENTAJE = 0.08

# Si el BPM que ya teníamos y el de Beatport difieren en menos que
# esto, los damos por "de acuerdo" (mismo criterio que analyze_bpm).
TOLERANCIA_BPM = 2.5

# El token anónimo dura 10 minutos; lo renovamos solo cuando expira.
_token = {"valor": None, "vence": 0.0}


def credenciales_del_embed():
    """Plan B: saca client_id/client_secret frescos del JavaScript del
    reproductor embebido (por si Beatport rotó los conocidos)."""
    try:
        pagina = requests.get(BEATPORT_EMBED, headers=NAVEGADOR, timeout=15).text
        bundle = re.search(r'src="(/static/main\.[0-9a-f]+\.js)"', pagina)
        if not bundle:
            return None
        js = requests.get(BEATPORT_EMBED.rstrip("/") + bundle.group(1), headers=NAVEGADOR, timeout=20).text
    except requests.RequestException:
        return None
    client_id = re.search(r'client_id.{0,24}?"([A-Za-z0-9]{30,})"', js)
    client_secret = re.search(r'client_secret.{0,24}?"([A-Za-z0-9]{60,})"', js)
    if client_id and client_secret:
        return client_id.group(1), client_secret.group(1)
    return None


def credenciales_posibles():
    yield CLIENT_ID, CLIENT_SECRET
    frescas = credenciales_del_embed()
    if frescas:
        yield frescas


def token_actual():
    """Devuelve un token anónimo vigente, o None si Beatport no dio
    ninguno (sin internet, o cambió el esquema del embed)."""
    if _token["valor"] and time.time() < _token["vence"]:
        return _token["valor"]
    for client_id, client_secret in credenciales_posibles():
        try:
            resp = requests.post(
                BEATPORT_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=15,
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        datos = resp.json()
        if datos.get("access_token"):
            _token["valor"] = datos["access_token"]
            # Renovamos un minuto antes de que venza, por las dudas.
            _token["vence"] = time.time() + datos.get("expires_in", 600) - 60
            return _token["valor"]
    return None


def buscar_en_beatport(artista, titulo, duracion_objetivo):
    """Busca el track en Beatport y devuelve el dict del track de la
    API que realmente coincide (artista, título y duración), o None.

    Levanta RuntimeError si nos quedamos sin token (para cortar la
    corrida en vez de imprimir "no encontrado" mil veces).
    """
    token = token_actual()
    if token is None:
        raise RuntimeError("Beatport no renovó el token anónimo")

    # Beatport separa el nombre del mix ("Juaan Remix") del título;
    # para la búsqueda usamos el título pelado y el mix lo chequeamos
    # después contra los candidatos.
    consulta = re.sub(r"\s*\([^)]*\)\s*$", "", titulo).strip() or titulo
    es_various = not artista or artista.lower() in ("various", "desconocido")
    params = {"name": consulta, "per_page": 20}
    if not es_various:
        params["artist_name"] = artista

    try:
        resp = requests.get(
            f"{BEATPORT_API}/catalog/tracks/",
            params=params,
            headers={"Authorization": f"Bearer {token}", **NAVEGADOR},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        resultados = resp.json().get("results") or []
    except (requests.RequestException, ValueError):
        return None

    mejores = []
    for track in resultados:
        nombre = track.get("name") or ""
        mix = (track.get("mix_name") or "").strip()
        # "Original Mix" no aporta nada; cualquier otro mix es parte
        # del título ("Concrete Jungle (Juaan Remix)").
        titulo_completo = nombre if mix.lower() in ("", "original mix", "original") else f"{nombre} ({mix})"
        if not se_parecen(titulo_completo, titulo):
            continue

        if not es_various:
            nombres = [a.get("name", "") for a in track.get("artists") or []]
            if not any(se_parecen(n, artista, umbral=0.8) for n in nombres):
                continue

        duracion_track = (track.get("length_ms") or 0) / 1000
        if duracion_objetivo and duracion_track:
            tolerancia = max(TOLERANCIA_SEG, duracion_objetivo * TOLERANCIA_PORCENTAJE)
            diferencia = abs(duracion_track - duracion_objetivo)
            if diferencia > tolerancia:
                continue
            mejores.append((diferencia, track))
        elif normalizar(titulo_completo) == normalizar(titulo):
            # Sin duración para comparar solo aceptamos el título
            # calcado (si no, entre "Tema" y "Tema (Remix)" agarra
            # cualquiera, y el remix tiene otro BPM y otra key).
            mejores.append((9999, track))

    if not mejores:
        return None
    return min(mejores, key=lambda par: par[0])[1]


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
    # Beatport se consulta para TODOS los tracks, tengan BPM o no:
    # es la fuente de referencia. Solo salteamos los que ya tienen
    # anotada una respuesta de Beatport en bpm_sources (aunque haya
    # sido "no está"), así las corridas siguientes van directo a lo
    # que falta.
    cursor.execute(
        """
        SELECT tracks.id, tracks.title, tracks.duration_display, tracks.bpm,
               tracks.bpm_source, tracks.bpm_alt, tracks.bpm_needs_review,
               tracks.bpm_verified, tracks.key, tracks.isrc,
               COALESCE(tracks.artist, releases.artist) AS artist
        FROM tracks
        JOIN releases ON releases.release_id = tracks.release_id
        WHERE tracks.key IS NULL
           OR NOT EXISTS (SELECT 1 FROM bpm_sources
                          WHERE track_id = tracks.id AND source = 'beatport')
        ORDER BY releases.artist, releases.title, tracks.id
        """
    )
    pendientes = cursor.fetchall()
    if limite:
        pendientes = pendientes[:limite]

    print(f"Tracks a consultar en Beatport: {len(pendientes)}\n")
    print("Conectando con Beatport (token anónimo del reproductor embebido)...")
    if token_actual() is None:
        print(
            "No pude conseguir el token anónimo de Beatport.\n"
            "Puede ser un problema de conexión, o que Beatport cambió su\n"
            "reproductor embebido. Probá de nuevo más tarde; mientras tanto\n"
            "el resto del flujo sigue andando (enrich_bpm.py, analyze_bpm.py)."
        )
        return
    print("Conectado.\n")

    stats = {"bpm": 0, "keys": 0, "coinciden": 0, "dudosos": 0, "isrc": 0}
    for i, row in enumerate(pendientes, start=1):
        # Si el disco tiene varios artistas los guardamos como
        # "Artista 1 / Artista 2"; para buscar usamos solo el primero.
        artista = row["artist"].split(" / ")[0]
        etiqueta = f"[{i}/{len(pendientes)}] {row['artist']} - {row['title']}"

        try:
            candidato = buscar_en_beatport(
                artista, row["title"], parsear_duracion(row["duration_display"])
            )
        except RuntimeError as e:
            print(f"\nCorto acá: {e}. Lo guardado hasta ahora no se pierde.")
            break

        if not candidato:
            # Anotamos que Beatport no lo tuvo (bpm en NULL), así la
            # próxima corrida no vuelve a preguntar. Si algún día
            # aparece en Beatport, borrá esa fila y volvé a correr.
            cursor.execute(
                "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm) VALUES (?, 'beatport', NULL)",
                (row["id"],),
            )
            conn.commit()
            print(f"{etiqueta} (no está en Beatport)")
            time.sleep(0.6)
            continue

        novedades = []

        mix = (candidato.get("mix_name") or "").strip()
        detalle = candidato.get("name") or ""
        if mix:
            detalle = f"{detalle} ({mix})"

        bpm_ficha = candidato.get("bpm")
        if bpm_ficha:
            # La ficha de Beatport a veces trae el tempo a la mitad
            # (67 en un track de 134): lo acomodamos al rango de tu
            # colección, dejando el número original anotado.
            bpm_ficha = float(bpm_ficha)
            bpm_beatport = acomodar_al_rango(bpm_ficha)
            if bpm_beatport != bpm_ficha:
                detalle = f"{detalle} (la ficha dice {bpm_ficha:g} BPM)"
            registrar_bpm_fuente(conn, row["id"], "beatport", bpm_beatport, detalle)
            if row["bpm"] is None:
                # Sin BPM previo: el de Beatport queda como principal,
                # pero SIN validar — la ✓ la ponés vos en el editor.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'beatport' WHERE id = ?",
                    (bpm_beatport, row["id"]),
                )
                stats["bpm"] += 1
                novedades.append(f"{bpm_beatport:g} BPM")
            elif row["bpm_source"] == "manual":
                # Lo cargaste vos: no se toca. La cifra de Beatport
                # queda visible como fuente en el editor.
                novedades.append(f"Beatport dice {bpm_beatport:g} (queda el tuyo)")
            elif abs(row["bpm"] - bpm_beatport) <= TOLERANCIA_BPM:
                # Beatport está de acuerdo: adoptamos su cifra (es la
                # oficial) y la duda queda resuelta, pero la validación
                # sigue siendo tuya, con un click en el editor.
                if row["bpm_verified"]:
                    novedades.append("Beatport coincide con tu valor validado")
                else:
                    cursor.execute(
                        "UPDATE tracks SET bpm = ?, bpm_source = 'beatport',"
                        " bpm_alt = NULL, bpm_needs_review = 0 WHERE id = ?",
                        (bpm_beatport, row["id"]),
                    )
                    stats["coinciden"] += 1
                    novedades.append("Beatport coincide (confirmalo en el editor)")
            elif row["bpm_verified"]:
                # Ya lo habías validado y Beatport dice otra cosa: no
                # pisamos tu valor, pero reabrimos la duda para que lo
                # mires con las dos cifras a la vista.
                cursor.execute(
                    "UPDATE tracks SET bpm_alt = ?, bpm_needs_review = 1,"
                    " bpm_verified = 0 WHERE id = ?",
                    (bpm_beatport, row["id"]),
                )
                stats["dudosos"] += 1
                novedades.append(
                    f"ojo: estaba validado en {row['bpm']:g} pero Beatport dice {bpm_beatport:g}"
                )
            else:
                # Difieren y el valor previo era automático: gana la
                # cifra oficial de Beatport, la otra queda a un click.
                cursor.execute(
                    "UPDATE tracks SET bpm = ?, bpm_source = 'beatport',"
                    " bpm_alt = ?, bpm_needs_review = 1 WHERE id = ?",
                    (bpm_beatport, row["bpm"], row["id"]),
                )
                stats["dudosos"] += 1
                novedades.append(
                    f"BPM dudoso (medido {row['bpm']:g}, Beatport dice {bpm_beatport:g})"
                )
        else:
            # Está en Beatport pero sin BPM cargado: lo anotamos para
            # no volver a preguntar.
            cursor.execute(
                "INSERT OR IGNORE INTO bpm_sources (track_id, source, bpm, detail)"
                " VALUES (?, 'beatport', NULL, ?)",
                (row["id"], detalle),
            )

        if row["key"] is None:
            key = normalizar_key((candidato.get("key") or {}).get("name"))
            if key:
                cursor.execute(
                    "UPDATE tracks SET key = ?, key_source = 'beatport' WHERE id = ?",
                    (key, row["id"]),
                )
                stats["keys"] += 1
                novedades.append(f"key {key} ({a_camelot(key)})")

        if not row["isrc"] and candidato.get("isrc"):
            cursor.execute(
                "UPDATE tracks SET isrc = ? WHERE id = ?",
                (candidato["isrc"], row["id"]),
            )
            stats["isrc"] += 1
            novedades.append("ISRC")

        conn.commit()
        print(f"{etiqueta} -> {', '.join(novedades) if novedades else 'sin novedades'}")

        # Vamos tranquilos, que la API es prestada.
        time.sleep(0.6)

    conn.close()

    print("\n" + "=" * 50)
    print(
        f"Beatport: {stats['bpm']} BPM nuevos, {stats['keys']} keys, "
        f"{stats['coinciden']} coinciden con lo medido, {stats['dudosos']} dudosos, "
        f"{stats['isrc']} ISRC."
    )
    print("Recordá: nada queda validado solo — la ✓ la ponés vos en el editor.")
    print("Próximo paso: python enrich_bandcamp.py  (tapas/duraciones que falten)")
    print("           o: python analyze_bpm.py      (mide lo que Beatport no tuvo)")
    print("           o: python edit_bpm.py         (validar BPMs, fuente por fuente)")


if __name__ == "__main__":
    main()
