"""
db.py — Manejo de la base de datos local.

Usamos SQLite porque viene incluido en Python (no hay que instalar
nada) y guarda todo en un solo archivo: vinyl_labels.db, que se crea
en esta misma carpeta la primera vez que corrés algo.

Pensalo como una versión mini de una hoja de cálculo con dos "tabs":
  - releases: un renglón por cada vinilo (LP)
  - tracks:   un renglón por cada canción de cada vinilo
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl_labels.db"


def get_connection():
    """Abre (o crea si no existe) la base de datos."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # permite acceder a columnas por nombre, ej: row["title"]
    return conn


def init_db():
    """
    Crea las tablas si todavía no existen. Es seguro correr esto
    las veces que quieras: si ya existen, no hace nada.
    """
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS releases (
            release_id   INTEGER PRIMARY KEY,   -- ID del disco en Discogs
            artist       TEXT,
            title        TEXT,
            year         INTEGER
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id        INTEGER NOT NULL,
            position          TEXT,     -- "A1", "A2", "B1", etc.
            title             TEXT,
            duration_display  TEXT,     -- "3:45" tal cual lo entrega Discogs
            bpm               REAL,     -- vacío (NULL) hasta que se complete
            bpm_source        TEXT,     -- "api", "manual", o NULL
            FOREIGN KEY (release_id) REFERENCES releases(release_id)
        );
        """
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # Esto te permite correr "python db.py" para chequear que
    # la base de datos se crea bien, sin tener que hacer nada más.
    init_db()
    print(f"Base de datos lista en: {DB_PATH}")
