# Vinyl Label Printer

Genera e imprime etiquetas con la lista de tracks de tus vinilos
(posición A1/A2/B1..., título, duración y BPM), a partir de tu
colección de Discogs, para pegar en la funda de cada disco.

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

   El `.env` no se sube a git, así que tus tokens quedan solo en tu
   computadora. Los ajustes técnicos (modelo de impresora, fuentes,
   etc.) siguen en `config.py`: tocá `PRINTER_MODEL` ahí si tu
   impresora no es la QL-800.

## Uso (cada vez que quieras generar etiquetas)

Corré estos scripts **en orden**, cada uno hace un solo paso:

```
python fetch_discogs.py    # 1. Trae tu colección de Discogs
python enrich_bpm.py       # 2. Busca BPM automáticamente donde puede
python analyze_bpm.py      # 2b. Mide los que faltan bajando el audio
                           #     de YouTube (¡lento pero efectivo!)
python bpm_manual.py export   # 3a. Exporta un CSV con los que faltan
#    -> completá el CSV a mano en Excel/Numbers, y después:
python bpm_manual.py import   # 3b. Carga lo que completaste
python render_labels.py    # 4. Genera las imágenes de las etiquetas
python print_labels.py     # 5. Imprime lo pendiente en la Brother QL
```

Notas:

- El paso 2 (Deezer) sirve para música conocida, pero para vinilos
  de sellos chicos Deezer no suele tener el BPM analizado. Ahí entra
  el paso 2b: busca cada track en YouTube (verificando que la
  duración coincida con la de Discogs), baja el audio a una carpeta
  temporal, mide el BPM localmente y borra el audio. Tarda ~30s por
  track y se puede cortar con Ctrl+C y retomar cuando quieras.
  Para probarlo primero con pocos: `python analyze_bpm.py 5`.
  Como toda medición automática puede pifiar (sobre todo al doble o
  a la mitad del tempo), los guardados quedan como `bpm_source =
  'youtube'` y cualquiera se corrige después con el paso 3.
- El paso 1 lo podés repetir cuando compres discos nuevos: actualiza
  y agrega sin duplicar, **sin perder los BPM que ya cargaste**, y
  saca de la base los discos que ya no estén en tu colección.
  Discogs limita los pedidos, así que tarda ~1 segundo por disco.
- El paso 4 puede generar solo algunos discos y mostrártelos antes
  de imprimir: `python render_labels.py aphex --ver` genera las
  etiquetas de los discos que contengan "aphex" y las abre en Vista
  Previa para que las chequees.
- El paso 5 tiene un **modo de prueba** que no necesita impresora:
  `python print_labels.py --prueba` te muestra qué etiquetas
  saldrían y cuántos centímetros de rollo usarían, sin imprimir ni
  gastar nada.
- El paso 5 solo imprime las etiquetas nuevas: las ya impresas se
  mueven a `labels_output/impresas/`. Para reimprimir una, movela de
  vuelta a `labels_output/`. También podés imprimir solo algunas:
  `python print_labels.py aphex` imprime las que contengan "aphex"
  en el nombre del archivo.

## Estructura del proyecto

```
.env                -> tus datos personales (tokens) — no se sube a git
config.py           -> ajustes técnicos (impresora, etiquetas, fuentes)
db.py               -> maneja la base de datos local (SQLite)
fetch_discogs.py    -> Paso 1
enrich_bpm.py       -> Paso 2
bpm_manual.py       -> Paso 3 (export/import CSV)
render_labels.py    -> Paso 4
print_labels.py     -> Paso 5
vinyl_labels.db     -> se crea solo, acá vive toda tu colección
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
