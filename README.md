# Vinyl Label Printer

Genera e imprime etiquetas con la tapa del disco, el sello, la fecha
de edición y la lista de tracks de tus vinilos (posición A1/A2/B1...,
título, duración, BPM y tonalidad en notación Camelot), a partir de
tu colección de Discogs, para pegar en la funda de cada disco.

Las fuentes de datos, en orden de prioridad:

1. **Discogs** — la fuente maestra: el disco, el sello, el catálogo,
   la fecha de edición, la tapa (de la edición real del vinilo) y la
   lista de tracks.
2. **Beatport** — BPM y tonalidad (key), lo suyo en música
   electrónica. Se consulta **para todos los tracks**, sí o sí: es la
   referencia de BPM. Sin cuenta ni API key.
3. **Medición propia (YouTube)** — el fallback de BPM: lo que
   Beatport no tiene se busca en Bandcamp/YouTube/SoundCloud, se baja
   el audio y se mide localmente con dos detectores.
4. **Deezer** — última red para BPM (opcional, rápido).
5. **Bandcamp** — respaldo para lo que falte (tapa, duraciones),
   ideal para música underground y sellos chicos. Sin cuenta.
6. **Spotify** — último respaldo (tapa, duraciones, ISRC). Opcional,
   requiere credenciales gratis.

El BPM de cada track guarda **de qué fuente salió**, y el editor
(paso 7) muestra todas las fuentes lado a lado. Nada se da por bueno
solo: **la validación es siempre manual** — la ✓ la ponés vos, track
por track, en el editor.

Pensado para imprimir en una **Brother QL** con rollo continuo de
62mm (DK-22205).

## Qué impresora comprar

- **Brother QL-800** (recomendada): rápida, barata, y soportada por
  todo el software. Es la opción segura.
- **Brother QL-600 / QL-600B**: también anda con este proyecto (más
  lenta y algo más barata). Si la elegís, poné `PRINTER_MODEL = "QL-600"`
  en `config.py`.

Las dos usan los mismos rollos DK. Para esto alcanza el **rollo
continuo blanco de 62mm (DK-22205)**: cada etiqueta sale del largo
exacto que necesite según la cantidad de tracks.

> Ojo: son impresoras térmicas directas, sin tinta. La impresión
> dura años pero se va desvaneciendo con el calor, el sol y el
> contacto directo con fundas de PVC blando. Para fundas de
> polietileno/papel no hay problema.

## Instalación (una sola vez)

1. Instalá Python 3 si no lo tenés (en Mac: `brew install python`).
2. En Mac, para que la conexión USB funcione: `brew install libusb`
3. Abrí la Terminal en esta carpeta y corré:
   ```
   make setup
   ```
   (instala las dependencias en un entorno propio del proyecto y te
   deja creado el archivo `.env`)
4. Copiá `.env.example` como `.env` y completá ahí tus datos
   personales (el `make setup` de abajo ya te lo copia solo):
   - Tu token de Discogs (`DISCOGS_USER_TOKEN`)
   - Tu usuario de Discogs (`DISCOGS_USERNAME`)
   - (Opcional) tu API key de getsongbpm.com (`GETSONGBPM_API_KEY`) —
     los BPM se buscan primero en Deezer, que es gratis y no pide
     ninguna clave, así que esto casi nunca hace falta.
   - (Opcional) las credenciales de una app de Spotify
     (`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, gratis en
     https://developer.spotify.com/dashboard) — sirven para el paso
     4, el último respaldo: tapa, duraciones e ISRC que ninguna otra
     fuente tuvo. Ojo: Spotify ya **no** da el BPM a las apps
     nuevas (bloqueado desde nov 2024), para eso están Beatport,
     Deezer y el análisis de audio. Beatport y Bandcamp no piden
     credenciales: esos pasos andan sin configurar nada.

   El `.env` no se sube a git, así que tus tokens quedan solo en tu
   computadora. Los ajustes técnicos (modelo de impresora, fuentes,
   etc.) siguen en `config.py`: tocá `PRINTER_MODEL` ahí si tu
   impresora no es la QL-800.

## Uso (cada vez que quieras generar etiquetas)

Corré estos scripts **en orden**, cada uno hace un solo paso:

```
python fetch_discogs.py    # 1. Trae tu colección y las tapas de Discogs
python enrich_beatport.py  # 2. BPM y tonalidad (key) desde Beatport,
                           #    para TODOS los tracks (la referencia)
python enrich_bandcamp.py  # 3. Tapas/duraciones que falten (Bandcamp)
python enrich_spotify.py   # 4. Último respaldo: tapa, duraciones e
                           #    ISRC desde Spotify (opcional)
python analyze_bpm.py      # 5. El fallback: mide el BPM de lo que
                           #    Beatport no tuvo, bajando el audio de
                           #    YouTube (¡lento pero efectivo!)
python enrich_bpm.py       # 6. Última red para BPM (Deezer, opcional)
python edit_bpm.py         # 7. El editor: cargar/corregir a mano y
                           #    VALIDAR cada BPM viendo sus fuentes
python render_labels.py    # 8. Genera las imágenes de las etiquetas
python print_labels.py     # 9. Imprime lo pendiente en la Brother QL
```

Notas:

- El paso 1 baja también la tapa de cada disco directo de Discogs
  (la foto de la edición real del vinilo, que sale impresa en el
  encabezado de la etiqueta, tramada a blanco y negro). Las tapas
  quedan en `covers/`, una por disco, y no se pisan: para rehacer
  una, borrá ese archivo de `covers/` y volvé a correr el paso. Las
  tapas muy oscuras o fotográficas pierden bastante al tramarse: la
  térmica no tiene grises.
- El paso 2 busca **cada track de tu colección** en Beatport — tenga
  BPM o no: es la fuente de referencia y se consulta sí o sí — y trae
  el BPM oficial y la tonalidad (key), que en la etiqueta sale en
  notación Camelot ("8A") para mezclar armónicamente. No necesita
  cuenta: usa el mismo acceso anónimo que el reproductor embebido de
  Beatport. Solo guarda datos si el candidato coincide en artista,
  título y duración con lo que dice Discogs. Cada respuesta queda
  anotada como fuente del track (visible en el editor). Si el track
  ya tenía un BPM medido y Beatport dice lo mismo, la duda queda
  resuelta (pero la ✓ la ponés vos en el paso 7); si dicen distinto,
  queda marcado como dudoso con el otro valor a un click. Lo que
  cargaste a mano nunca se pisa.
- El paso 3 (Bandcamp) completa lo que a Discogs le falte — tapa y
  duraciones — buscando el álbum con la API pública del buscador de
  bandcamp.com. Para vinilos underground y de sellos chicos suele
  ser la única fuente que los tiene. Bandcamp no publica BPM ni key.
- El paso 4 (Spotify, opcional) es el último respaldo: tapa,
  duraciones e ISRC que sigan faltando. Si el disco no está en
  Spotify — normal con vinilos de nicho — no pasa nada, ya casi
  todo vino de los pasos anteriores.
- El paso 5 es el **fallback de Beatport**: busca cada track sin BPM
  en Bandcamp, YouTube o SoundCloud (en ese orden), verificando que
  la duración coincida con la de Discogs, baja el audio a una carpeta
  temporal, mide el BPM localmente y borra el audio. Tarda ~30s por
  track y se puede cortar con Ctrl+C y retomar cuando quieras. Si
  YouTube se pone en modo anti-bot ("Sign in to confirm you're not a
  bot"), el script sigue por SoundCloud solo; YouTube se destraba
  solo en unas horas, o al toque si configurás
  `YOUTUBE_COOKIES_NAVEGADOR` en `config.py`. Evitá correr dos
  análisis a la vez, que es lo que despierta al anti-bot.
  Para probarlo primero con pocos: `python analyze_bpm.py 5`.
  El tempo se mide con **dos detectores** (deeprhythm, una red
  neuronal muy precisa en música electrónica, y librosa): si
  coinciden, el número es de fiar; si no —el clásico error de medir
  89 donde el tempo real es 134— el track queda marcado como
  *dudoso* y en el paso 7 lo resolvés con un click, con el otro
  candidato ahí nomás como botón.
- El paso 6 (Deezer, opcional) es la última red: busca BPM para lo
  que ni Beatport ni la medición pudieron. Sirve para música
  conocida; para vinilos de sellos chicos Deezer no suele tener el
  BPM analizado.
- Si ya tenías BPMs medidos con la versión vieja del análisis (un
  solo detector), corré **una vez** `python audit_bpm.py`: re-mide
  todos los BPM automáticos viejos, corrige los que estaban mal
  medidos y anota la re-medición como fuente — todo queda listo
  para validar en el paso 7.
- El paso 7 (`make editar`) levanta una página local (solo la ve tu
  computadora) con toda la colección: buscador, un casillero de BPM
  y otro de key por track, y cada cambio se guarda solo. La key la
  podés escribir en Camelot ("8A") o musical ("Am", "f# minor").
  Lo que cargás ahí queda como `manual` y nada lo pisa. Si preferís
  planilla, el viejo flujo CSV sigue disponible:
  `python bpm_manual.py export` / `import`.
- El paso 7 es donde se **valida**: cada track muestra, como
  píldoras, todas las fuentes de las que salió un BPM ("beatport
  128" · "youtube 127.9" · "deezer 128") con el detalle de dónde
  salió cada número. La ✓ verde de "validado" **nunca se pone sola**,
  ni aunque todas las fuentes coincidan: la ponés vos, con el botón ✓
  (el valor actual está bien) o clickeando la píldora de una fuente
  (ese valor pasa a ser el BPM del track y queda validado, porque lo
  elegiste vos viendo todas las opciones). Escribir un BPM a mano
  también cuenta como validarlo. Los dudosos (fuentes en desacuerdo)
  quedan resaltados con el valor alternativo a un click. El objetivo
  es ver arriba "colección completa: N/N BPM validados ✓" — ahí
  estás para imprimir tranquilo. En las etiquetas, un BPM dudoso sin
  confirmar sale con asterisco (ej: "129*").
- El paso 1 lo podés repetir cuando compres discos nuevos: actualiza
  y agrega sin duplicar, **sin perder los BPM que ya cargaste**, y
  saca de la base los discos que ya no estén en tu colección.
  Discogs limita los pedidos, así que tarda ~1 segundo por disco.
- El paso 8 puede generar solo algunos discos y mostrártelos antes
  de imprimir: `python render_labels.py aphex --ver` genera las
  etiquetas de los discos que contengan "aphex" y las abre en Vista
  Previa para que las chequees.
- El paso 9 tiene un **modo de prueba** que no necesita impresora:
  `python print_labels.py --prueba` te muestra qué etiquetas
  saldrían y cuántos centímetros de rollo usarían, sin imprimir ni
  gastar nada.
- El paso 9 solo imprime las etiquetas nuevas: las ya impresas se
  mueven a `labels_output/impresas/`. Para reimprimir una, movela de
  vuelta a `labels_output/`. También podés imprimir solo algunas:
  `python print_labels.py aphex` imprime las que contengan "aphex"
  en el nombre del archivo.

## Estructura del proyecto

```
.env                -> tus datos personales (tokens) — no se sube a git
config.py           -> ajustes técnicos (impresora, etiquetas, fuentes)
db.py               -> maneja la base de datos local (SQLite)
comunes.py          -> helpers compartidos (matching, tapas, keys)
fetch_discogs.py    -> Paso 1 (colección + tapas, fuente maestra)
enrich_beatport.py  -> Paso 2 (BPM y key desde Beatport, sí o sí)
enrich_bandcamp.py  -> Paso 3 (tapas/duraciones que falten)
enrich_spotify.py   -> Paso 4 (último respaldo — opcional)
analyze_bpm.py      -> Paso 5 (el fallback: mide el BPM del audio)
enrich_bpm.py       -> Paso 6 (última red: Deezer — opcional)
audit_bpm.py        -> re-chequeo de lo medido con la versión vieja
edit_bpm.py         -> Paso 7 (editor y validador de BPM y key)
bpm_manual.py       -> Paso 7 alternativo (export/import CSV)
render_labels.py    -> Paso 8
print_labels.py     -> Paso 9
vinyl_labels.db     -> se crea solo, acá vive toda tu colección
covers/             -> tapas bajadas (una por disco)
labels_output/      -> imágenes generadas pendientes de imprimir
labels_output/impresas/ -> las que ya salieron por la impresora
```

## Problemas comunes

- **"No encuentro la fuente configurada"**: abrí `config.py` y
  cambiá `FONT_PATH` por la ruta de alguna fuente .ttf que sí tengas
  instalada. El script sigue funcionando igual, solo se ve más feo.

- **La impresora no imprime / no la detecta**: fijate que esté
  enchufada y encendida, y en Mac que hayas instalado libusb. Si
  sigue sin aparecer, corré `brother_ql discover`, copiá el
  identificador que te muestre y pegalo en `config.py`, en
  `PRINTER_IDENTIFIER`.

- **Modo "Editor Lite"**: si tu impresora tiene ese botón activado
  (una luz prendida), hay que apagarlo manteniendo el botón apretado
  unos segundos — bloquea la impresión por USB.

- **Muchos tracks sin BPM**: es normal, sobre todo con ediciones de
  nicho o vinilos viejos. Usá `bpm_manual.py export` / `import` para
  completarlos vos mismo con Shazam, Tunebat, o tu oído. Ojo también
  con los BPM automáticos en música electrónica: a veces vienen al
  doble o a la mitad del tempo real (70 en vez de 140).
