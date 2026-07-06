"""
edit_bpm.py — STEP 7: BPM and tonality editor and validator

Launches a local page (only visible on your computer) with your entire
collection, to load or correct BPMs and keys by hand without exporting and
importing CSVs: find the record, click on the BPM (or key), type the value
and done — it's saved directly to the database.

This is also where you VALIDATE: each track shows all the sources where
a BPM came from (beatport, YouTube measurement, Deezer...) with its value,
side by side. Nothing validates itself — you always put the green checkmark,
either with the ✓ button (current value is correct) or by clicking on a
source pill (use that value and validate it in the same click).

The key is shown in Camelot notation ("8A"), but you can write it any way:
"8A", "Am", "f# minor" — it's saved normalized.

How to run it:
    python edit_bpm.py

It opens automatically in the browser (http://localhost:8765). To close it,
go back to the terminal and press Ctrl+C.

BPMs and keys you enter here become source 'manual' and always win: neither
fetch_discogs nor automatic searchers overwrite them.
"""

import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from comunes import a_camelot, normalizar_key
from db import get_connection, init_db, registrar_bpm_fuente

COVERS_DIR = Path(__file__).parent / config.COVERS_DIR

PUERTO = 8765

PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vinyl BPM Editor</title>
<style>
  :root {
    --fondo: #f5f4f0; --tarjeta: #ffffff; --tinta: #1a1a1a;
    --gris: #767370; --linea: #e4e2dd; --acento: #b0433a; --ok: #2e7d4f;
    --duda: #b07d1e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --fondo: #191817; --tarjeta: #232120; --tinta: #ece9e4;
      --gris: #98938d; --linea: #3a3733; --acento: #e07a6b; --ok: #6fbf8f;
      --duda: #d9a94a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--fondo); color: var(--tinta);
    font: 15px/1.45 -apple-system, "Helvetica Neue", sans-serif;
  }
  header {
    position: sticky; top: 0; background: var(--fondo);
    padding: 18px 20px 12px; border-bottom: 1px solid var(--linea);
    display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
  }
  h1 { font-size: 17px; margin: 0; }
  #resumen { color: var(--gris); font-size: 13px; }
  #controles { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  #buscar {
    font: inherit; padding: 6px 10px; width: 220px; color: var(--tinta);
    border: 1px solid var(--linea); border-radius: 7px; background: var(--tarjeta);
  }
  label.chk { font-size: 13px; color: var(--gris); user-select: none; cursor: pointer; }
  main { max-width: 860px; margin: 0 auto; padding: 18px 20px 80px; }
  .disco {
    background: var(--tarjeta); border: 1px solid var(--linea);
    border-radius: 10px; margin-bottom: 14px; overflow: hidden;
  }
  .disco h2 {
    font-size: 14px; margin: 0; padding: 10px 14px;
    border-bottom: 1px solid var(--linea);
    display: flex; align-items: center; gap: 10px;
  }
  .disco h2 small { color: var(--gris); font-weight: normal; }
  img.tapa {
    width: 44px; height: 44px; border-radius: 5px; object-fit: cover;
    border: 1px solid var(--linea); flex-shrink: 0;
  }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 7px 8px; border-top: 1px solid var(--linea); }
  th {
    font-size: 11px; color: var(--gris); font-weight: 600;
    text-transform: uppercase; letter-spacing: .05em;
    text-align: left; padding: 8px 8px 2px;
  }
  th.pos { padding-left: 14px; }
  th.dur { text-align: right; }
  th.key { text-align: center; }
  td.pos { width: 44px; color: var(--gris); padding-left: 14px; font-variant-numeric: tabular-nums; }
  td.dur { width: 60px; color: var(--gris); text-align: right; font-variant-numeric: tabular-nums; }
  td.bpm { width: 86px; }
  td.src { padding-right: 8px; }
  input.bpm {
    width: 72px; font: inherit; font-weight: 600; text-align: right;
    padding: 4px 8px; color: var(--tinta); background: transparent;
    border: 1px solid var(--linea); border-radius: 7px;
  }
  input.bpm:focus { outline: 2px solid var(--acento); border-color: transparent; }
  input.bpm.vacio { border-style: dashed; border-color: var(--acento); }
  input.bpm.guardado { outline: 2px solid var(--ok); border-color: transparent; }
  input.bpm.dudoso { border-color: var(--duda); border-width: 2px; }
  td.key { width: 64px; }
  input.key {
    width: 52px; font: inherit; text-align: center; padding: 4px 4px;
    color: var(--tinta); background: transparent;
    border: 1px solid var(--linea); border-radius: 7px;
  }
  input.key:focus { outline: 2px solid var(--acento); border-color: transparent; }
  input.key.guardado { outline: 2px solid var(--ok); border-color: transparent; }
  button.fuente {
    font: inherit; font-size: 11px; color: var(--gris); cursor: pointer;
    border: 1px solid var(--linea); border-radius: 20px; padding: 1px 8px;
    white-space: nowrap; background: transparent; margin: 1px 4px 1px 0;
  }
  button.fuente b { font-weight: 600; }
  button.fuente.activa { color: var(--tinta); border-color: var(--gris); }
  button.fuente:hover { color: var(--acento); border-color: var(--acento); }
  td.rev { width: 120px; padding-right: 14px; white-space: nowrap; }
  button.alt, button.confirmar {
    font: inherit; font-size: 12px; cursor: pointer; border-radius: 7px;
    padding: 3px 9px; background: transparent; margin-right: 6px;
  }
  button.alt { color: var(--duda); border: 1px solid var(--duda); font-weight: 600; }
  button.confirmar { color: var(--ok); border: 1px solid var(--ok); }
  button.alt:hover, button.confirmar:hover { filter: brightness(1.15); }
  .verificado {
    color: var(--ok); font-size: 12px; font-weight: 600; white-space: nowrap;
  }
  #nada { color: var(--gris); text-align: center; padding: 40px 0; }
  #resumen .listo { color: var(--ok); font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>Vinyl BPM Editor</h1>
  <span id="resumen"></span>
  <div id="controles">
    <label class="chk"><input type="checkbox" id="soloSin"> solo sin BPM</label>
    <label class="chk"><input type="checkbox" id="soloDudosos"> solo dudosos</label>
    <label class="chk"><input type="checkbox" id="soloSinValidar"> solo sin validar</label>
    <input id="buscar" type="search" placeholder="buscar disco o track…">
  </div>
</header>
<main id="lista"></main>
<div id="nada" hidden>Nada que mostrar con ese filtro.</div>

<script>
let datos = { releases: [] };

async function cargar() {
  const r = await fetch('/api/data');
  datos = await r.json();
  pintar();
}

function pintar() {
  const filtro = document.getElementById('buscar').value.toLowerCase();
  const soloSin = document.getElementById('soloSin').checked;
  const soloDudosos = document.getElementById('soloDudosos').checked;
  const soloSinValidar = document.getElementById('soloSinValidar').checked;
  const lista = document.getElementById('lista');
  lista.textContent = '';
  let pendientes = 0, dudosos = 0, sinValidar = 0, validados = 0, total = 0, visibles = 0;

  for (const disco of datos.releases) {
    const enDisco = (disco.artist + ' ' + disco.title).toLowerCase().includes(filtro);
    let tracks = disco.tracks.filter(t =>
      (enDisco || t.title.toLowerCase().includes(filtro) ||
        (t.artist || '').toLowerCase().includes(filtro)) &&
      (!soloSin || t.bpm === null) &&
      (!soloDudosos || t.review) &&
      (!soloSinValidar || (t.bpm !== null && !t.verified)));
    total += disco.tracks.length;
    pendientes += disco.tracks.filter(t => t.bpm === null).length;
    dudosos += disco.tracks.filter(t => t.review).length;
    sinValidar += disco.tracks.filter(t => t.bpm !== null && !t.verified).length;
    validados += disco.tracks.filter(t => t.verified).length;
    if (!tracks.length) continue;
    visibles++;

    const caja = document.createElement('section');
    caja.className = 'disco';
    const h = document.createElement('h2');
    if (disco.cover) {
      const im = document.createElement('img');
      im.className = 'tapa';
      im.src = '/covers/' + disco.id + '.jpg';
      im.loading = 'lazy';
      im.alt = '';
      h.appendChild(im);
    }
    const nombre = document.createElement('span');
    nombre.textContent = disco.artist + ' — ' + disco.title + ' ';
    if (disco.year) {
      const s = document.createElement('small');
      s.textContent = '(' + disco.year + ')';
      nombre.appendChild(s);
    }
    h.appendChild(nombre);
    caja.appendChild(h);

    const tabla = document.createElement('table');
    const titulos = document.createElement('tr');
    for (const [cls, txt] of [['pos', ''], ['', ''], ['dur', 'dur'],
                              ['bpm', 'BPM'], ['key', 'key'], ['src', 'fuentes'], ['rev', '']]) {
      const th = document.createElement('th');
      th.className = cls; th.textContent = txt; titulos.appendChild(th);
    }
    tabla.appendChild(titulos);
    for (const t of tracks) {
      const tr = document.createElement('tr');
      const celda = (cls, txt) => {
        const td = document.createElement('td');
        td.className = cls; td.textContent = txt; tr.appendChild(td); return td;
      };
      celda('pos', t.position);
      celda('', t.artist ? t.artist + ' – ' + t.title : t.title);
      celda('dur', t.duration || '');
      const tdBpm = celda('bpm', '');
      const inp = document.createElement('input');
      inp.className = 'bpm' + (t.bpm === null ? ' vacio' : '') + (t.review ? ' dudoso' : '');
      inp.placeholder = '?';
      inp.inputMode = 'decimal';
      inp.value = t.bpm === null ? '' : t.bpm;
      inp.dataset.id = t.id;
      inp.addEventListener('change', guardar);
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') e.target.blur(); });
      tdBpm.appendChild(inp);
      const tdKey = celda('key', '');
      const kin = document.createElement('input');
      kin.className = 'key';
      kin.value = t.camelot || '';
      kin.dataset.id = t.id;
      kin.title = t.key
        ? 'Key ' + t.key + (t.key_source ? ' (' + t.key_source + ')' : '')
        : 'Tonalidad: en Camelot ("8A") o musical ("Am")';
      kin.addEventListener('change', guardarKey);
      kin.addEventListener('keydown', e => { if (e.key === 'Enter') e.target.blur(); });
      tdKey.appendChild(kin);
      // Una píldora por cada fuente que dio un BPM, con su valor.
      // La de la fuente actual va resaltada. Click en cualquiera:
      // ese valor pasa a ser EL BPM del track y queda validado por
      // vos (la validación siempre es tuya, nunca automática).
      const tdSrc = celda('src', '');
      const ordenFuentes = { manual: 0, beatport: 1, youtube: 2, deezer: 3, getsongbpm: 4 };
      const fuentes = (t.sources || []).slice()
        .sort((a, b) => (ordenFuentes[a.source] ?? 9) - (ordenFuentes[b.source] ?? 9));
      for (const f of fuentes) {
        const b = document.createElement('button');
        b.className = 'fuente' + (t.source === f.source ? ' activa' : '');
        b.append(f.source + ' ');
        const val = document.createElement('b');
        val.textContent = f.bpm;
        b.appendChild(val);
        b.title = (f.detail ? f.detail + '\\n' : '') +
          'Click: usar ' + f.bpm + ' BPM (' + f.source + ') y darlo por validado';
        b.addEventListener('click', () => usarFuente(t.id, f.source));
        tdSrc.appendChild(b);
      }
      const tdRev = celda('rev', '');
      if (t.review) {
        // El otro detector midió distinto: un click y queda ese valor.
        if (t.alt !== null) {
          const b = document.createElement('button');
          b.className = 'alt';
          b.textContent = '¿' + t.alt + '?';
          b.title = 'El otro detector midió ' + t.alt + ' — click para usar ese valor';
          b.addEventListener('click', () => guardarValor(t.id, t.alt));
          tdRev.appendChild(b);
        }
        const ok = document.createElement('button');
        ok.className = 'confirmar';
        ok.textContent = '✓';
        ok.title = 'El BPM actual está bien: validarlo';
        ok.addEventListener('click', () => confirmar(t.id));
        tdRev.appendChild(ok);
      } else if (t.verified) {
        // Validado: lo confirmaste vos (la ✓ nunca es automática).
        const v = document.createElement('span');
        v.className = 'verificado';
        v.textContent = '✓ validado';
        v.title = 'BPM validado por vos';
        tdRev.appendChild(v);
      } else if (t.bpm !== null) {
        // Tiene BPM pero nadie lo validó: un click lo da por bueno.
        const ok = document.createElement('button');
        ok.className = 'confirmar';
        ok.textContent = '✓';
        ok.title = 'Sin validar: mirá las fuentes y, si te cierra, click para validarlo';
        ok.addEventListener('click', () => confirmar(t.id));
        tdRev.appendChild(ok);
      }
      tabla.appendChild(tr);
    }
    caja.appendChild(tabla);
    lista.appendChild(caja);
  }

  document.getElementById('nada').hidden = visibles > 0;
  const resumen = document.getElementById('resumen');
  if (total && validados === total) {
    resumen.innerHTML = '<span class="listo">colección completa: ' +
      total + '/' + total + ' BPM validados ✓</span>';
  } else {
    const partes = [validados + '/' + total + ' validados'];
    if (pendientes) partes.push(pendientes + ' sin BPM');
    if (dudosos) partes.push(dudosos + ' dudosos');
    if (sinValidar - dudosos > 0) partes.push((sinValidar - dudosos) + ' sin validar');
    resumen.textContent = partes.join(' · ');
  }
}

async function guardarValor(id, bpm) {
  const r = await fetch('/api/bpm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, bpm: bpm }),
  });
  if (!r.ok) return false;
  for (const disco of datos.releases)
    for (const t of disco.tracks)
      if (t.id == id) {
        t.bpm = bpm; t.source = bpm === null ? null : 'manual';
        t.review = 0; t.alt = null; t.verified = bpm === null ? 0 : 1;
        t.sources = (t.sources || []).filter(f => f.source !== 'manual');
        if (bpm !== null) t.sources.push({ source: 'manual', bpm: bpm, detail: null });
      }
  return true;
}

async function usarFuente(id, source) {
  const r = await fetch('/api/fuente', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, source: source }),
  });
  if (!r.ok) return;
  const res = await r.json();
  for (const disco of datos.releases)
    for (const t of disco.tracks)
      if (t.id == id) {
        t.bpm = res.bpm; t.source = source;
        t.review = 0; t.alt = null; t.verified = 1;
      }
  pintar();
}

async function guardar(e) {
  const inp = e.target;
  const texto = inp.value.trim().replace(',', '.');
  let bpm = null;
  if (texto !== '') {
    bpm = parseFloat(texto);
    if (isNaN(bpm) || bpm < 30 || bpm > 300) { inp.select(); return; }
  }
  if (!await guardarValor(parseInt(inp.dataset.id), bpm)) return;
  inp.classList.toggle('vacio', bpm === null);
  inp.classList.add('guardado');
  setTimeout(() => inp.classList.remove('guardado'), 900);
  pintar();
}

async function guardarKey(e) {
  const inp = e.target;
  const r = await fetch('/api/key', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: parseInt(inp.dataset.id), key: inp.value.trim() }),
  });
  if (!r.ok) { inp.select(); return; }  // no se entendió la key: corregila
  const res = await r.json();
  for (const disco of datos.releases)
    for (const t of disco.tracks)
      if (t.id == inp.dataset.id) {
        t.key = res.key; t.camelot = res.camelot;
        t.key_source = res.key ? 'manual' : null;
      }
  inp.value = res.camelot;
  inp.title = res.key ? 'Key ' + res.key + ' (manual)' : 'Tonalidad: en Camelot ("8A") o musical ("Am")';
  inp.classList.add('guardado');
  setTimeout(() => inp.classList.remove('guardado'), 900);
}

async function confirmar(id) {
  const r = await fetch('/api/confirmar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id }),
  });
  if (!r.ok) return;
  for (const disco of datos.releases)
    for (const t of disco.tracks)
      if (t.id == id) { t.review = 0; t.alt = null; t.verified = 1; }
  pintar();
}

document.getElementById('buscar').addEventListener('input', pintar);
document.getElementById('soloSin').addEventListener('change', pintar);
document.getElementById('soloDudosos').addEventListener('change', pintar);
document.getElementById('soloSinValidar').addEventListener('change', pintar);

// Refresca cada 20s (por si analyze_bpm.py está corriendo en paralelo),
// salvo que estés escribiendo un BPM o una key en ese momento.
setInterval(() => {
  const activo = document.activeElement;
  if (!activo || !(activo.classList.contains('bpm') || activo.classList.contains('key'))) cargar();
}, 20000);

cargar();
</script>
</body>
</html>
"""


def leer_datos():
    conn = get_connection()
    cursor = conn.cursor()

    # Todas las fuentes que dieron un BPM, por track (las filas con
    # bpm en NULL son "consultada y no estaba": no se muestran).
    cursor.execute("SELECT track_id, source, bpm, detail FROM bpm_sources WHERE bpm IS NOT NULL")
    fuentes = {}
    for f in cursor.fetchall():
        fuentes.setdefault(f["track_id"], []).append(
            {"source": f["source"], "bpm": f["bpm"], "detail": f["detail"]}
        )

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = []
    for release in cursor.fetchall():
        cursor.execute(
            "SELECT id, position, title, artist, duration_display, bpm, bpm_source,"
            "       bpm_alt, bpm_needs_review, bpm_verified, key, key_source"
            " FROM tracks WHERE release_id = ? ORDER BY id",
            (release["release_id"],),
        )
        tracks = [
            {
                "id": t["id"],
                "position": t["position"],
                "title": t["title"],
                "artist": t["artist"],
                "duration": t["duration_display"],
                "bpm": t["bpm"],
                "source": t["bpm_source"],
                "sources": fuentes.get(t["id"], []),
                "alt": t["bpm_alt"],
                "review": t["bpm_needs_review"] or 0,
                "verified": t["bpm_verified"] or 0,
                "key": t["key"],
                "camelot": a_camelot(t["key"]),
                "key_source": t["key_source"],
            }
            for t in cursor.fetchall()
        ]
        if tracks:
            # La tapa la baja enrich_spotify.py; solo la anunciamos si
            # el archivo existe de verdad, para no mostrar imágenes rotas.
            tiene_tapa = bool(release["cover_path"]) and (
                COVERS_DIR / f"{release['release_id']}.jpg"
            ).exists()
            releases.append(
                {
                    "id": release["release_id"],
                    "artist": release["artist"],
                    "title": release["title"],
                    "year": release["year"],
                    "cover": tiene_tapa,
                    "tracks": tracks,
                }
            )
    conn.close()
    return {"releases": releases}


def guardar_bpm(track_id, bpm):
    # Un BPM que escribiste vos cuenta como validado (escribirlo ES la
    # validación manual); si lo borrás, obviamente deja de estarlo.
    conn = get_connection()
    conn.execute(
        "UPDATE tracks SET bpm = ?, bpm_source = ?,"
        " bpm_alt = NULL, bpm_needs_review = 0, bpm_verified = ? WHERE id = ?",
        (bpm, "manual" if bpm is not None else None, int(bpm is not None), track_id),
    )
    if bpm is not None:
        registrar_bpm_fuente(conn, track_id, "manual", bpm)
    else:
        conn.execute(
            "DELETE FROM bpm_sources WHERE track_id = ? AND source = 'manual'",
            (track_id,),
        )
    conn.commit()
    conn.close()


def usar_fuente(track_id, fuente):
    """El usuario eligió el valor de una fuente (click en la píldora):
    ese BPM pasa a ser el del track y queda validado — elegirlo a mano
    ES la confirmación. Devuelve el BPM, o None si la fuente no tenía."""
    conn = get_connection()
    row = conn.execute(
        "SELECT bpm FROM bpm_sources WHERE track_id = ? AND source = ? AND bpm IS NOT NULL",
        (track_id, fuente),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    conn.execute(
        "UPDATE tracks SET bpm = ?, bpm_source = ?, bpm_alt = NULL,"
        " bpm_needs_review = 0, bpm_verified = 1 WHERE id = ?",
        (row["bpm"], fuente, track_id),
    )
    conn.commit()
    conn.close()
    return row["bpm"]


def guardar_key(track_id, key):
    """Una key que escribiste vos pisa lo que haya; si la borrás,
    queda vacía (y el próximo enrich_beatport.py puede rellenarla)."""
    conn = get_connection()
    conn.execute(
        "UPDATE tracks SET key = ?, key_source = ? WHERE id = ?",
        (key, "manual" if key is not None else None, track_id),
    )
    conn.commit()
    conn.close()


def confirmar_bpm(track_id):
    """El usuario revisó el track y el BPM guardado está bien: queda
    validado (la fuente queda como estaba)."""
    conn = get_connection()
    conn.execute(
        "UPDATE tracks SET bpm_alt = NULL, bpm_needs_review = 0,"
        " bpm_verified = 1 WHERE id = ?",
        (track_id,),
    )
    conn.commit()
    conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # sin ruido en la terminal

    def responder(self, cuerpo, tipo="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", f"{tipo}; charset=utf-8")
        self.end_headers()
        self.wfile.write(cuerpo.encode("utf-8"))

    def do_GET(self):
        if self.path == "/":
            self.responder(PAGINA, "text/html")
        elif self.path == "/api/data":
            self.responder(json.dumps(leer_datos()))
        elif re.fullmatch(r"/covers/\d+\.jpg", self.path):
            # Sirve las tapas bajadas por enrich_spotify.py. El patrón
            # solo acepta "/covers/<número>.jpg", así que no hay riesgo
            # de que alguien pida otros archivos de la máquina.
            archivo = COVERS_DIR / Path(self.path).name
            if archivo.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(archivo.read_bytes())
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in ("/api/bpm", "/api/confirmar", "/api/key", "/api/fuente"):
            self.send_error(404)
            return
        largo = int(self.headers.get("Content-Length", 0))
        try:
            pedido = json.loads(self.rfile.read(largo))
            track_id = int(pedido["id"])
        except (ValueError, KeyError, json.JSONDecodeError):
            self.send_error(400)
            return

        if self.path == "/api/fuente":
            # Adoptar el BPM de una fuente (y validarlo, porque lo
            # elegiste vos mirando todas las opciones).
            fuente = str(pedido.get("source") or "")
            bpm = usar_fuente(track_id, fuente)
            if bpm is None:
                self.send_error(400)  # esa fuente no tiene BPM para este track
                return
            self.responder(json.dumps({"ok": True, "bpm": bpm}))
            return

        if self.path == "/api/key":
            # Acepta "8A", "Am", "f# minor"...; vacío = borrarla.
            texto = str(pedido.get("key") or "").strip()
            key = normalizar_key(texto) if texto else None
            if texto and key is None:
                self.send_error(400)  # no se entendió la tonalidad
                return
            guardar_key(track_id, key)
            self.responder(json.dumps({"ok": True, "key": key, "camelot": a_camelot(key)}))
            return

        if self.path == "/api/confirmar":
            confirmar_bpm(track_id)
        else:
            try:
                bpm = pedido.get("bpm")
                if bpm is not None:
                    bpm = float(bpm)
                    if not 30 <= bpm <= 300:
                        raise ValueError(bpm)
            except ValueError:
                self.send_error(400)
                return
            guardar_bpm(track_id, bpm)
        self.responder(json.dumps({"ok": True}))


def main():
    init_db()
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Handler)
    except OSError:
        print(
            f"El puerto {PUERTO} ya está ocupado: seguramente el editor ya\n"
            f"está abierto en otra terminal (o quedó corriendo de antes).\n"
            f"Entrá a http://localhost:{PUERTO} — y si acabás de actualizar\n"
            f"el proyecto, cerrá aquel con Ctrl+C y volvé a correr este."
        )
        return
    url = f"http://localhost:{PUERTO}"
    print(f"Editor de BPM abierto en {url}")
    print("(si no se abrió solo, entrá a esa dirección en el navegador)")
    print("Para terminar: Ctrl+C\n")
    threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nListo. Todos los cambios ya estaban guardados.")


if __name__ == "__main__":
    main()
