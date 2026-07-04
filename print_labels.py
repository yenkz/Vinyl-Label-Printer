"""
print_labels.py — PASO 5

Toma las imágenes de labels_output/ y las manda a imprimir, una por
una, a tu impresora Brother QL conectada por USB.

Cada etiqueta que se imprime bien se mueve a labels_output/impresas/,
así la próxima vez solo se imprimen las nuevas. Si querés reimprimir
alguna, movela de vuelta a labels_output/ y corré esto de nuevo.

Cómo correrlo:
    python print_labels.py            # imprime todo lo pendiente
    python print_labels.py aphex      # solo las que contengan "aphex"
    python print_labels.py --prueba   # modo de prueba: muestra qué se
                                      # imprimiría y de qué largo, sin
                                      # impresora y sin gastar etiqueta
                                      # (se puede combinar con el filtro)

Antes de correrlo por primera vez:
  - Conectá la impresora por USB, encendida y con el rollo continuo
    de 62mm puesto.
  - Si la impresora tiene modo "Editor Lite" (QL-600B, QL-700...),
    desactivalo (mantené apretado el botón hasta que se apague la
    luz), porque bloquea la impresión por USB.
"""

import sys
from pathlib import Path

from PIL import Image
from brother_ql import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import discover, send

import config

OUTPUT_DIR = Path(__file__).parent / config.OUTPUT_DIR
IMPRESAS_DIR = OUTPUT_DIR / "impresas"


def encontrar_impresora():
    """Devuelve el identificador de la impresora: el de config.py si
    está definido, o el primer dispositivo Brother que aparezca por USB."""
    if config.PRINTER_IDENTIFIER:
        return config.PRINTER_IDENTIFIER

    try:
        dispositivos = discover(backend_identifier=config.PRINTER_BACKEND)
    except Exception as e:
        print(f"No pude buscar impresoras por USB: {e}")
        dispositivos = []

    if not dispositivos:
        print(
            "\nNo encontré ninguna impresora conectada.\n"
            "Fijate que esté enchufada y encendida. Si sigue sin aparecer,\n"
            "corré en la terminal:  brother_ql discover\n"
            "y pegá el identificador en config.py (PRINTER_IDENTIFIER)."
        )
        return None

    identificador = dispositivos[0]["identifier"]
    print(f"Impresora detectada: {identificador}\n")
    return identificador


def main():
    argumentos = sys.argv[1:]
    modo_prueba = "--prueba" in argumentos
    filtro = next((a.lower() for a in argumentos if not a.startswith("--")), "")

    imagenes = sorted(p for p in OUTPUT_DIR.glob("*.png") if filtro in p.name.lower())

    if not imagenes:
        if filtro:
            print(f"No hay etiquetas pendientes que contengan '{filtro}'.")
        else:
            print(f"No hay etiquetas pendientes en {OUTPUT_DIR}/.")
        print("Las ya impresas están en labels_output/impresas/ (movelas")
        print("de vuelta a labels_output/ si querés reimprimirlas), y para")
        print("generar nuevas corré: python render_labels.py")
        return

    if modo_prueba:
        print(f"MODO DE PRUEBA: {len(imagenes)} etiquetas pendientes. No se imprime nada.\n")
    else:
        print(f"Encontradas {len(imagenes)} etiquetas para imprimir.\n")

        printer_identifier = encontrar_impresora()
        if printer_identifier is None:
            return

        respuesta = input(f"¿Imprimir las {len(imagenes)} etiquetas ahora? (s/n): ").strip().lower()
        if respuesta != "s":
            print("Cancelado. No se imprimió nada.")
            return

        IMPRESAS_DIR.mkdir(exist_ok=True)

    impresas = 0
    largo_total_mm = 0
    for i, ruta in enumerate(imagenes, start=1):
        try:
            img = Image.open(ruta)
            # ~11.8 píxeles por mm (300dpi): con esto estimamos cuánto
            # rollo consume cada etiqueta.
            largo_mm = round(img.height / 11.81)
            largo_total_mm += largo_mm

            if modo_prueba:
                print(f"[{i}/{len(imagenes)}] {ruta.name}  ({largo_mm}mm de rollo)")
            else:
                print(f"[{i}/{len(imagenes)}] Imprimiendo {ruta.name}...")

            # El objeto raster acumula instrucciones, así que usamos
            # uno nuevo por etiqueta.
            qlr = BrotherQLRaster(config.PRINTER_MODEL)
            instructions = convert(
                qlr,
                [img],
                label="62",  # rollo continuo de 62mm
                rotate="auto",
                threshold=70,
                cut=True,
            )

            if modo_prueba:
                # Hasta acá validamos que la etiqueta se convierte bien
                # al formato de la impresora; solo falta mandarla.
                continue

            send(
                instructions=instructions,
                printer_identifier=printer_identifier,
                backend_identifier=config.PRINTER_BACKEND,
                blocking=True,
            )
        except Exception as e:
            print(f"   -> Error con esta etiqueta, sigo con el resto: {e}")
            continue

        # Si llegamos acá, salió bien: la movemos a impresas/ para
        # que no se vuelva a imprimir la próxima vez.
        ruta.rename(IMPRESAS_DIR / ruta.name)
        impresas += 1

    if modo_prueba:
        print(f"\nEn total se usarían unos {largo_total_mm / 10:.0f}cm de rollo.")
        print("Para imprimir de verdad: python print_labels.py")
    else:
        print(f"\nListo. {impresas} de {len(imagenes)} etiquetas impresas.")
        if impresas < len(imagenes):
            print("Las que fallaron quedaron en labels_output/ para reintentar.")


if __name__ == "__main__":
    main()
