"""
config.py — Ajustes técnicos del proyecto.

Acá NO va nada personal: tus tokens y tu usuario viven en el archivo
.env (copiá .env.example como .env y completalo). Este archivo solo
tiene la configuración de la impresora y de las etiquetas, que en
general no hace falta tocar.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Carga las variables del archivo .env que está junto a este archivo.
load_dotenv(Path(__file__).parent / ".env")

# =========================================================
# 1) DATOS PERSONALES (vienen del archivo .env)
# =========================================================
DISCOGS_USER_TOKEN = os.environ.get("DISCOGS_USER_TOKEN", "")
DISCOGS_USERNAME = os.environ.get("DISCOGS_USERNAME", "")
GETSONGBPM_API_KEY = os.environ.get("GETSONGBPM_API_KEY", "")

# Identificador de tu "aplicación" ante Discogs. Podés dejarlo tal
# cual, no requiere ningún registro.
DISCOGS_USER_AGENT = "VinylLabelPrinter/1.0"

# =========================================================
# 2) IMPRESORA (Brother QL series)
# =========================================================
# Recomendada: QL-800 (más rápida y mejor soportada). Si en cambio
# compraste una QL-600 / QL-600B, poné "QL-600" acá — también anda.
PRINTER_MODEL = "QL-800"

# Backend de conexión. Con USB casi siempre alcanza con "pyusb".
PRINTER_BACKEND = "pyusb"

# Identificador exacto de la impresora. Dejalo en None para que
# el script la busque solo entre los dispositivos USB conectados.
# Si falla, corré en la terminal:
#   brother_ql discover
# y pegá acá el string que te devuelva, algo como:
#   "usb://0x04f9:0x209b/000A1Z123456"
PRINTER_IDENTIFIER = None

# =========================================================
# 3) ETIQUETAS
# =========================================================
# Las Brother QL imprimen en rollo continuo de 62mm de ancho.
# Esto NO se debe cambiar salvo que compres otro tipo de rollo.
LABEL_WIDTH_MM = 62
LABEL_WIDTH_PX = 696  # ancho imprimible en píxeles a 300dpi (fijo)

# Carpeta donde se van a guardar las imágenes generadas, listas
# para imprimir.
OUTPUT_DIR = "labels_output"

# Fuente (tipografía) a usar en las etiquetas. Si no existe en tu
# computadora, el script cae automáticamente a una fuente básica.
# En Mac, una opción segura suele ser:
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_PATH_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
