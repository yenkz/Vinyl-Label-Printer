"""
config.py — Technical settings for the project.

No personal data here: your tokens and username live in the .env file
(copy .env.example as .env and fill it in). This file only contains
printer and label configuration, which you generally don't need to touch.
"""

import os

from dotenv import load_dotenv

from .paths import PROJECT_ROOT

# Load variables from the .env file in this directory.
load_dotenv(PROJECT_ROOT / ".env")

# =========================================================
# 1) PERSONAL DATA (comes from the .env file)
# =========================================================
DISCOGS_USER_TOKEN = os.environ.get("DISCOGS_USER_TOKEN", "")
# OPTIONAL. Spotify app (https://developer.spotify.com/dashboard)
# to enrich labels: album cover, missing durations from Discogs, and ISRC.
# Note: new Spotify apps do NOT have BPM access (audio-features blocked since Nov 2024).
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# OPTIONAL. Soulseek download (download_music.py), via a local slskd
# daemon. See the README section "Download digital copies (Soulseek)".
SLSKD_HOST = os.environ.get("SLSKD_HOST", "http://localhost:5030")
SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY", "")
SLSKD_URL_BASE = os.environ.get("SLSKD_URL_BASE", "/")
# Where slskd drops finished downloads (must match slskd's own "downloads"
# directory) and where we build the organized library.
SLSKD_DOWNLOADS_DIR = os.environ.get("SLSKD_DOWNLOADS_DIR", "~/Music/Vinyl/_incoming")
MUSIC_DIR = os.environ.get("MUSIC_DIR", "~/Music/Vinyl")

# Your "application" identifier with Discogs. You can leave it as is,
# no registration required.
DISCOGS_USER_AGENT = "VinylLabelPrinter/1.0"

# =========================================================
# 2) PRINTER (Brother QL series)
# =========================================================
# Recommended: QL-800 (faster and better supported). If you bought
# a QL-600 / QL-600B instead, set "QL-600" here — it also works.
PRINTER_MODEL = "QL-800"

# Connection backend. USB usually works fine with "pyusb".
PRINTER_BACKEND = "pyusb"

# Exact printer identifier. Leave as None to have the script search
# for it among connected USB devices. If it fails, run in the terminal:
#   brother_ql discover
# and paste the returned string here, something like:
#   "usb://0x04f9:0x209b/000A1Z123456"
PRINTER_IDENTIFIER = None

# =========================================================
# 3) BPM ANALYSIS (analyze_bpm.py)
# =========================================================
# If YouTube enters "confirm you're not a robot" mode (happens when
# downloading many audio files in a row), the script automatically falls back
# to SoundCloud. If you also want YouTube to work immediately, put here
# the browser where you're logged into YouTube so it can use your cookies:
# "chrome", "safari", "firefox", "brave" or "edge".
# (With Safari, Terminal may ask for "Full Disk Access" in System Settings.)
# Empty = disabled.
YOUTUBE_COOKIES_BROWSER = ""

# =========================================================
# 4) LABELS
# =========================================================
# Brother QL printers print on continuous 62mm wide rolls. This project sends
# jobs in the two-color mode required by the DK-2251 black/red-on-white roll.
# Do NOT change this unless you buy a different type of roll.
LABEL_WIDTH_MM = 62
LABEL_WIDTH_PX = 696  # printable width in pixels at 300dpi (fixed)

# Folder where generated images will be saved, ready to print.
OUTPUT_DIR = "labels_output"

# Folder where downloaded covers are saved (one per record).
COVERS_DIR = "covers"

# Font (typeface) to use on labels. If it doesn't exist on your
# computer, the script automatically falls back to a basic font.
# On Mac, a safe option is usually:
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_PATH_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
