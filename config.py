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

# OPCIONAL. App de Spotify (https://developer.spotify.com/dashboard)
# para enriquecer las etiquetas: tapa del disco, duraciones que le
# falten a Discogs, e ISRC. Ojo: las apps nuevas de Spotify NO dan
# acceso al BPM (audio-features está bloqueado desde nov 2024).
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

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
# 3) ANÁLISIS DE BPM (analyze_bpm.py)
# =========================================================
# Si YouTube se pone en modo "confirmá que no sos un robot" (pasa
# cuando bajás muchos audios seguidos), el script pasa solo a buscar
# en SoundCloud. Si además querés que YouTube vuelva a andar ya,
# poné acá el navegador donde estés logueado en YouTube, para que
# use tus cookies: "chrome", "safari", "firefox", "brave" o "edge".
# (Con Safari, la Terminal puede pedir permiso de "Acceso total al
# disco" en Ajustes del Sistema.)  Vacío = apagado.
YOUTUBE_COOKIES_NAVEGADOR = ""

# =========================================================
# 4) ETIQUETAS
# =========================================================
# Las Brother QL imprimen en rollo continuo de 62mm de ancho.
# Esto NO se debe cambiar salvo que compres otro tipo de rollo.
LABEL_WIDTH_MM = 62
LABEL_WIDTH_PX = 696  # ancho imprimible en píxeles a 300dpi (fijo)

# Carpeta donde se van a guardar las imágenes generadas, listas
# para imprimir.
OUTPUT_DIR = "labels_output"

# Carpeta donde se guardan las tapas bajadas (una por disco).
COVERS_DIR = "covers"

# Fuente (tipografía) a usar en las etiquetas. Si no existe en tu
# computadora, el script cae automáticamente a una fuente básica.
# En Mac, una opción segura suele ser:
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_PATH_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
