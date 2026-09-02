"""
edit_bpm.py — STEP 6: BPM and tonality editor and validator

Launches a local page (only visible on your computer) with your entire
collection, to load or correct BPMs and keys by hand without exporting and
importing CSVs: find the record, click on the BPM (or key), type the value
and done — it's saved directly to the database.

This is also where you validate the values that still need review. Each track
shows all BPM sources (Beatport, YouTube measurement, or historical sources)
side by side. Discogs + Beatport matches are already confirmed automatically;
for the remaining values, use the ✓ button or click a source pill.

The key is shown in Camelot notation ("8A"), but you can write it any way:
"8A", "Am", "f# minor" — it's saved normalized. Automatic Essentia/librosa
key candidates appear below the field; detector disagreements are highlighted
and can be resolved by choosing a candidate or confirming the current one.

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
from common import to_camelot, normalize_key
from db import get_connection, init_db, record_bpm_source, record_key_source

COVERS_DIR = Path(__file__).parent / config.COVERS_DIR

PORT = 8765

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vinyl BPM &amp; Key Editor</title>
<style>
  :root {
    --bg: #f5f4f0; --card: #ffffff; --ink: #1a1a1a;
    --muted: #767370; --line: #e4e2dd; --accent: #b0433a; --ok: #2e7d4f;
    --warn: #b07d1e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #191817; --card: #232120; --ink: #ece9e4;
      --muted: #98938d; --line: #3a3733; --accent: #e07a6b; --ok: #6fbf8f;
      --warn: #d9a94a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.45 -apple-system, "Helvetica Neue", sans-serif;
  }
  header {
    position: sticky; top: 0; background: var(--bg);
    padding: 18px 20px 12px; border-bottom: 1px solid var(--line);
    display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
  }
  h1 { font-size: 17px; margin: 0; }
  #summary { color: var(--muted); font-size: 13px; }
  #controls { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  #search {
    font: inherit; padding: 6px 10px; width: 220px; color: var(--ink);
    border: 1px solid var(--line); border-radius: 7px; background: var(--card);
  }
  label.chk { font-size: 13px; color: var(--muted); user-select: none; cursor: pointer; }
  main { max-width: 1080px; margin: 0 auto; padding: 18px 20px 80px; }
  .release {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; margin-bottom: 14px; overflow: hidden;
  }
  .release h2 {
    font-size: 14px; margin: 0; padding: 10px 14px;
    border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 10px;
  }
  .release h2 small { color: var(--muted); font-weight: normal; }
  img.cover {
    width: 44px; height: 44px; border-radius: 5px; object-fit: cover;
    border: 1px solid var(--line); flex-shrink: 0;
  }
  table { width: 100%; border-collapse: collapse; }
  td { padding: 7px 8px; border-top: 1px solid var(--line); }
  th {
    font-size: 11px; color: var(--muted); font-weight: 600;
    text-transform: uppercase; letter-spacing: .05em;
    text-align: left; padding: 8px 8px 2px;
  }
  th.pos { padding-left: 14px; }
  th.dur { text-align: right; }
  th.key { text-align: center; }
  td.pos { width: 44px; color: var(--muted); padding-left: 14px; font-variant-numeric: tabular-nums; }
  td.dur { width: 60px; color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; }
  td.bpm { width: 86px; }
  td.src { padding-right: 8px; }
  input.bpm {
    width: 72px; font: inherit; font-weight: 600; text-align: right;
    padding: 4px 8px; color: var(--ink); background: transparent;
    border: 1px solid var(--line); border-radius: 7px;
  }
  input.bpm:focus { outline: 2px solid var(--accent); border-color: transparent; }
  input.bpm.empty { border-style: dashed; border-color: var(--accent); }
  input.bpm.saved { outline: 2px solid var(--ok); border-color: transparent; }
  input.bpm.doubtful { border-color: var(--warn); border-width: 2px; }
  td.key { width: 180px; text-align: center; }
  input.key {
    width: 52px; font: inherit; text-align: center; padding: 4px 4px;
    color: var(--ink); background: transparent;
    border: 1px solid var(--line); border-radius: 7px;
  }
  input.key:focus { outline: 2px solid var(--accent); border-color: transparent; }
  input.key.saved { outline: 2px solid var(--ok); border-color: transparent; }
  input.key.doubtful { border-color: var(--warn); border-width: 2px; }
  .key-options { margin-top: 3px; white-space: nowrap; }
  button.key-source, button.key-confirm {
    font: inherit; font-size: 10px; cursor: pointer; background: transparent;
    border: 1px solid var(--line); border-radius: 12px; padding: 0 5px;
    color: var(--muted); margin: 1px 2px 0 0;
  }
  button.key-source.active { color: var(--ink); border-color: var(--muted); }
  button.key-source:hover { color: var(--accent); border-color: var(--accent); }
  button.key-confirm { color: var(--ok); border-color: var(--ok); }
  button.source {
    font: inherit; font-size: 11px; color: var(--muted); cursor: pointer;
    border: 1px solid var(--line); border-radius: 20px; padding: 1px 8px;
    white-space: nowrap; background: transparent; margin: 1px 4px 1px 0;
  }
  button.source b { font-weight: 600; }
  button.source.active { color: var(--ink); border-color: var(--muted); }
  button.source:hover { color: var(--accent); border-color: var(--accent); }
  td.rev { width: 120px; padding-right: 14px; white-space: nowrap; }
  button.alt, button.confirm {
    font: inherit; font-size: 12px; cursor: pointer; border-radius: 7px;
    padding: 3px 9px; background: transparent; margin-right: 6px;
  }
  button.alt { color: var(--warn); border: 1px solid var(--warn); font-weight: 600; }
  button.confirm { color: var(--ok); border: 1px solid var(--ok); }
  button.alt:hover, button.confirm:hover { filter: brightness(1.15); }
  .verified {
    color: var(--ok); font-size: 12px; font-weight: 600; white-space: nowrap;
  }
  #empty { color: var(--muted); text-align: center; padding: 40px 0; }
  #summary .done { color: var(--ok); font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>Vinyl BPM &amp; Key Editor</h1>
  <span id="summary"></span>
  <div id="controls">
    <label class="chk"><input type="checkbox" id="onlyMissing"> only missing BPM</label>
    <label class="chk"><input type="checkbox" id="onlyMissingKey"> only missing key</label>
    <label class="chk"><input type="checkbox" id="onlyDoubtful"> only doubtful</label>
    <label class="chk"><input type="checkbox" id="onlyUnvalidated"> only unvalidated</label>
    <input id="search" type="search" placeholder="search record or track…">
  </div>
</header>
<main id="list"></main>
<div id="empty" hidden>Nothing to show for that filter.</div>

<script>
let data = { releases: [] };

async function load() {
  const r = await fetch('/api/data');
  data = await r.json();
  render();
}

function render() {
  const query = document.getElementById('search').value.toLowerCase();
  const onlyMissing = document.getElementById('onlyMissing').checked;
  const onlyMissingKey = document.getElementById('onlyMissingKey').checked;
  const onlyDoubtful = document.getElementById('onlyDoubtful').checked;
  const onlyUnvalidated = document.getElementById('onlyUnvalidated').checked;
  const list = document.getElementById('list');
  list.textContent = '';
  let pending = 0, keyPending = 0, bpmDoubtful = 0, keyDoubtful = 0;
  let unvalidated = 0, validated = 0, total = 0, visible = 0;

  for (const release of data.releases) {
    const inRelease = (release.artist + ' ' + release.title).toLowerCase().includes(query);
    let tracks = release.tracks.filter(t =>
      (inRelease || t.title.toLowerCase().includes(query) ||
        (t.artist || '').toLowerCase().includes(query)) &&
      (!onlyMissing || t.bpm === null) &&
      (!onlyMissingKey || t.key === null) &&
      (!onlyDoubtful || t.review || t.key_review) &&
      (!onlyUnvalidated || (t.bpm !== null && !t.verified)));
    total += release.tracks.length;
    pending += release.tracks.filter(t => t.bpm === null).length;
    keyPending += release.tracks.filter(t => t.key === null).length;
    bpmDoubtful += release.tracks.filter(t => t.review).length;
    keyDoubtful += release.tracks.filter(t => t.key_review).length;
    unvalidated += release.tracks.filter(t => t.bpm !== null && !t.verified).length;
    validated += release.tracks.filter(t => t.verified).length;
    if (!tracks.length) continue;
    visible++;

    const box = document.createElement('section');
    box.className = 'release';
    const h = document.createElement('h2');
    if (release.cover) {
      const im = document.createElement('img');
      im.className = 'cover';
      im.src = '/covers/' + release.id + '.jpg';
      im.loading = 'lazy';
      im.alt = '';
      h.appendChild(im);
    }
    const name = document.createElement('span');
    name.textContent = release.artist + ' — ' + release.title + ' ';
    if (release.year) {
      const s = document.createElement('small');
      s.textContent = '(' + release.year + ')';
      name.appendChild(s);
    }
    h.appendChild(name);
    box.appendChild(h);

    const table = document.createElement('table');
    const headers = document.createElement('tr');
    for (const [cls, txt] of [['pos', ''], ['', ''], ['dur', 'dur'],
                              ['bpm', 'BPM'], ['key', 'key'], ['src', 'sources'], ['rev', '']]) {
      const th = document.createElement('th');
      th.className = cls; th.textContent = txt; headers.appendChild(th);
    }
    table.appendChild(headers);
    for (const t of tracks) {
      const tr = document.createElement('tr');
      const cell = (cls, txt) => {
        const td = document.createElement('td');
        td.className = cls; td.textContent = txt; tr.appendChild(td); return td;
      };
      cell('pos', t.position);
      cell('', t.artist ? t.artist + ' – ' + t.title : t.title);
      cell('dur', t.duration || '');
      const tdBpm = cell('bpm', '');
      const inp = document.createElement('input');
      inp.className = 'bpm' + (t.bpm === null ? ' empty' : '') + (t.review ? ' doubtful' : '');
      inp.placeholder = '?';
      inp.inputMode = 'decimal';
      inp.value = t.bpm === null ? '' : t.bpm;
      inp.dataset.id = t.id;
      inp.addEventListener('change', onBpmChange);
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') e.target.blur(); });
      tdBpm.appendChild(inp);
      const tdKey = cell('key', '');
      const kin = document.createElement('input');
      kin.className = 'key' + (t.key_review ? ' doubtful' : '');
      kin.value = t.camelot || '';
      kin.dataset.id = t.id;
      kin.title = t.key
        ? 'Key ' + t.key + (t.key_source ? ' (' + t.key_source + ')' : '')
        : 'Key: Camelot ("8A") or musical ("Am")';
      kin.addEventListener('change', onKeyChange);
      kin.addEventListener('keydown', e => { if (e.key === 'Enter') e.target.blur(); });
      tdKey.appendChild(kin);
      const keyOptions = document.createElement('div');
      keyOptions.className = 'key-options';
      const keySourceOrder = { manual: 0, beatport: 1, essentia: 2, librosa: 3 };
      const keySources = (t.key_sources || []).slice()
        .sort((a, b) => (keySourceOrder[a.source] ?? 9) - (keySourceOrder[b.source] ?? 9));
      for (const f of keySources) {
        const b = document.createElement('button');
        b.className = 'key-source' +
          ((t.key_source === f.source || (t.key_source === 'audio' && f.key === t.key))
            ? ' active' : '');
        b.textContent = f.source + ' ' + f.camelot;
        const score = f.strength === null ? '' : ' · score ' + Number(f.strength).toFixed(3);
        b.title = (f.detail ? f.detail + '\\n' : '') + 'Key ' + f.key + score +
          '\\nClick to choose and confirm this key';
        b.addEventListener('click', () => useKeySource(t.id, f.source));
        keyOptions.appendChild(b);
      }
      if (t.key_review && t.key !== null) {
        const ok = document.createElement('button');
        ok.className = 'key-confirm';
        ok.textContent = '✓';
        ok.title = 'The current key is right: confirm it';
        ok.addEventListener('click', () => confirmKey(t.id));
        keyOptions.appendChild(ok);
      }
      tdKey.appendChild(keyOptions);
      // One pill per source that reported a BPM, with its value. The one
      // for the current source is highlighted. Click any of them: that
      // value becomes THE track's BPM and is validated by you.
      const tdSrc = cell('src', '');
      const sourceOrder = { manual: 0, beatport: 1, youtube: 2 };
      const sources = (t.sources || []).slice()
        .sort((a, b) => (sourceOrder[a.source] ?? 9) - (sourceOrder[b.source] ?? 9));
      for (const f of sources) {
        const b = document.createElement('button');
        b.className = 'source' + (t.source === f.source ? ' active' : '');
        b.append(f.source + ' ');
        const val = document.createElement('b');
        val.textContent = f.bpm;
        b.appendChild(val);
        b.title = (f.detail ? f.detail + '\\n' : '') +
          'Click: use ' + f.bpm + ' BPM (' + f.source + ') and mark it validated';
        b.addEventListener('click', () => useSource(t.id, f.source));
        tdSrc.appendChild(b);
      }
      const tdRev = cell('rev', '');
      if (t.review) {
        // The other detector measured something different: one click and
        // that value is used.
        if (t.alt !== null) {
          const b = document.createElement('button');
          b.className = 'alt';
          b.textContent = t.alt + '?';
          b.title = 'The other detector measured ' + t.alt + ' — click to use that value';
          b.addEventListener('click', () => saveValue(t.id, t.alt));
          tdRev.appendChild(b);
        }
        const ok = document.createElement('button');
        ok.className = 'confirm';
        ok.textContent = '✓';
        ok.title = 'The current BPM is right: validate it';
        ok.addEventListener('click', () => confirmTrack(t.id));
        tdRev.appendChild(ok);
      } else if (t.verified) {
        // Validated either by a Discogs + Beatport match or by the user.
        const v = document.createElement('span');
        v.className = 'verified';
        v.textContent = '✓ validated';
        v.title = t.source === 'beatport'
          ? 'Automatically confirmed by matching Discogs with Beatport'
          : 'BPM validated by you';
        tdRev.appendChild(v);
      } else if (t.bpm !== null) {
        // Has a BPM but nobody validated it: one click marks it good.
        const ok = document.createElement('button');
        ok.className = 'confirm';
        ok.textContent = '✓';
        ok.title = 'Unvalidated: check the sources and, if it looks right, click to validate it';
        ok.addEventListener('click', () => confirmTrack(t.id));
        tdRev.appendChild(ok);
      }
      table.appendChild(tr);
    }
    box.appendChild(table);
    list.appendChild(box);
  }

  document.getElementById('empty').hidden = visible > 0;
  const summary = document.getElementById('summary');
  if (total && validated === total && keyPending === 0 && keyDoubtful === 0) {
    summary.innerHTML = '<span class="done">collection complete: ' +
      total + '/' + total + ' BPM validated · keys analyzed ✓</span>';
  } else {
    const parts = [validated + '/' + total + ' validated'];
    if (pending) parts.push(pending + ' without BPM');
    if (keyPending) parts.push(keyPending + ' without key');
    if (bpmDoubtful) parts.push(bpmDoubtful + ' doubtful BPM');
    if (keyDoubtful) parts.push(keyDoubtful + ' doubtful key');
    if (unvalidated - bpmDoubtful > 0) parts.push((unvalidated - bpmDoubtful) + ' unvalidated');
    summary.textContent = parts.join(' · ');
  }
}

async function saveValue(id, bpm) {
  const r = await fetch('/api/bpm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, bpm: bpm }),
  });
  if (!r.ok) return false;
  for (const release of data.releases)
    for (const t of release.tracks)
      if (t.id == id) {
        t.bpm = bpm; t.source = bpm === null ? null : 'manual';
        t.review = 0; t.alt = null; t.verified = bpm === null ? 0 : 1;
        t.sources = (t.sources || []).filter(f => f.source !== 'manual');
        if (bpm !== null) t.sources.push({ source: 'manual', bpm: bpm, detail: null });
      }
  return true;
}

async function useSource(id, source) {
  const r = await fetch('/api/source', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, source: source }),
  });
  if (!r.ok) return;
  const res = await r.json();
  for (const release of data.releases)
    for (const t of release.tracks)
      if (t.id == id) {
        t.bpm = res.bpm; t.source = source;
        t.review = 0; t.alt = null; t.verified = 1;
      }
  render();
}

async function onBpmChange(e) {
  const inp = e.target;
  const text = inp.value.trim().replace(',', '.');
  let bpm = null;
  if (text !== '') {
    bpm = parseFloat(text);
    if (isNaN(bpm) || bpm < 30 || bpm > 300) { inp.select(); return; }
  }
  if (!await saveValue(parseInt(inp.dataset.id), bpm)) return;
  inp.classList.toggle('empty', bpm === null);
  inp.classList.add('saved');
  setTimeout(() => inp.classList.remove('saved'), 900);
  render();
}

async function onKeyChange(e) {
  const inp = e.target;
  const r = await fetch('/api/key', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: parseInt(inp.dataset.id), key: inp.value.trim() }),
  });
  if (!r.ok) { inp.select(); return; }  // key not understood: fix it
  const res = await r.json();
  for (const release of data.releases)
    for (const t of release.tracks)
      if (t.id == inp.dataset.id) {
        t.key = res.key; t.camelot = res.camelot;
        t.key_source = res.key ? 'manual' : null;
        t.key_alt = null; t.key_alt_camelot = '';
        t.key_review = 0; t.key_verified = res.key ? 1 : 0; t.key_strength = null;
        t.key_sources = (t.key_sources || []).filter(f => f.source !== 'manual');
        if (res.key) t.key_sources.push({
          source: 'manual', key: res.key, camelot: res.camelot,
          strength: null, detail: null,
        });
      }
  inp.value = res.camelot;
  inp.title = res.key ? 'Key ' + res.key + ' (manual)' : 'Key: Camelot ("8A") or musical ("Am")';
  inp.classList.add('saved');
  setTimeout(() => inp.classList.remove('saved'), 900);
  render();
}

async function useKeySource(id, source) {
  const r = await fetch('/api/key-source', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, source: source }),
  });
  if (!r.ok) return;
  const res = await r.json();
  for (const release of data.releases)
    for (const t of release.tracks)
      if (t.id == id) {
        const chosen = (t.key_sources || []).find(f => f.source === source);
        t.key = res.key; t.camelot = res.camelot; t.key_source = source;
        t.key_alt = null; t.key_alt_camelot = ''; t.key_review = 0;
        t.key_verified = 1; t.key_strength = chosen ? chosen.strength : null;
      }
  render();
}

async function confirmKey(id) {
  const r = await fetch('/api/key-confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id }),
  });
  if (!r.ok) return;
  for (const release of data.releases)
    for (const t of release.tracks)
      if (t.id == id) {
        t.key_alt = null; t.key_alt_camelot = '';
        t.key_review = 0; t.key_verified = 1;
      }
  render();
}

async function confirmTrack(id) {
  const r = await fetch('/api/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id }),
  });
  if (!r.ok) return;
  for (const release of data.releases)
    for (const t of release.tracks)
      if (t.id == id) { t.review = 0; t.alt = null; t.verified = 1; }
  render();
}

document.getElementById('search').addEventListener('input', render);
document.getElementById('onlyMissing').addEventListener('change', render);
document.getElementById('onlyMissingKey').addEventListener('change', render);
document.getElementById('onlyDoubtful').addEventListener('change', render);
document.getElementById('onlyUnvalidated').addEventListener('change', render);

// Refresh every 20s (in case analyze_bpm.py is running in parallel),
// unless you're typing a BPM or a key at that moment.
setInterval(() => {
  const active = document.activeElement;
  if (!active || !(active.classList.contains('bpm') || active.classList.contains('key'))) load();
}, 20000);

load();
</script>
</body>
</html>
"""


def read_data():
    conn = get_connection()
    cursor = conn.cursor()

    # Every source that reported a BPM, per track (rows with bpm NULL are
    # "consulted and didn't have it": those are not shown).
    cursor.execute("SELECT track_id, source, bpm, detail FROM bpm_sources WHERE bpm IS NOT NULL")
    sources_by_track = {}
    for f in cursor.fetchall():
        sources_by_track.setdefault(f["track_id"], []).append(
            {"source": f["source"], "bpm": f["bpm"], "detail": f["detail"]}
        )

    cursor.execute(
        "SELECT track_id, source, key, strength, detail"
        " FROM key_sources WHERE key IS NOT NULL"
    )
    key_sources_by_track = {}
    for f in cursor.fetchall():
        key_sources_by_track.setdefault(f["track_id"], []).append(
            {
                "source": f["source"],
                "key": f["key"],
                "camelot": to_camelot(f["key"]),
                "strength": f["strength"],
                "detail": f["detail"],
            }
        )

    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = []
    for release in cursor.fetchall():
        cursor.execute(
            "SELECT id, position, title, artist, duration_display, bpm, bpm_source,"
            "       bpm_alt, bpm_needs_review, bpm_verified, key, key_source,"
            "       key_alt, key_needs_review, key_verified, key_strength"
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
                "sources": sources_by_track.get(t["id"], []),
                "alt": t["bpm_alt"],
                "review": t["bpm_needs_review"] or 0,
                "verified": t["bpm_verified"] or 0,
                "key": t["key"],
                "camelot": to_camelot(t["key"]),
                "key_source": t["key_source"],
                "key_sources": key_sources_by_track.get(t["id"], []),
                "key_alt": t["key_alt"],
                "key_alt_camelot": to_camelot(t["key_alt"]),
                "key_review": t["key_needs_review"] or 0,
                "key_verified": t["key_verified"] or 0,
                "key_strength": t["key_strength"],
            }
            for t in cursor.fetchall()
        ]
        if tracks:
            # The cover is downloaded by enrich_spotify.py; we only announce
            # it if the file actually exists, to avoid showing broken images.
            has_cover = bool(release["cover_path"]) and (
                COVERS_DIR / f"{release['release_id']}.jpg"
            ).exists()
            releases.append(
                {
                    "id": release["release_id"],
                    "artist": release["artist"],
                    "title": release["title"],
                    "year": release["year"],
                    "cover": has_cover,
                    "tracks": tracks,
                }
            )
    conn.close()
    return {"releases": releases}


def save_bpm(track_id, bpm):
    # A BPM you typed yourself counts as validated (typing it IS the manual
    # validation); if you clear it, it obviously stops being validated.
    conn = get_connection()
    conn.execute(
        "UPDATE tracks SET bpm = ?, bpm_source = ?,"
        " bpm_alt = NULL, bpm_needs_review = 0, bpm_verified = ? WHERE id = ?",
        (bpm, "manual" if bpm is not None else None, int(bpm is not None), track_id),
    )
    if bpm is not None:
        record_bpm_source(conn, track_id, "manual", bpm)
    else:
        conn.execute(
            "DELETE FROM bpm_sources WHERE track_id = ? AND source = 'manual'",
            (track_id,),
        )
    conn.commit()
    conn.close()


def use_source(track_id, source):
    """The user chose a source's value (click on the pill): that BPM becomes
    the track's and is validated — choosing it by hand IS the confirmation.
    Returns the BPM, or None if the source didn't have one."""
    conn = get_connection()
    row = conn.execute(
        "SELECT bpm FROM bpm_sources WHERE track_id = ? AND source = ? AND bpm IS NOT NULL",
        (track_id, source),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    conn.execute(
        "UPDATE tracks SET bpm = ?, bpm_source = ?, bpm_alt = NULL,"
        " bpm_needs_review = 0, bpm_verified = 1 WHERE id = ?",
        (row["bpm"], source, track_id),
    )
    conn.commit()
    conn.close()
    return row["bpm"]


def save_key(track_id, key):
    """A key you typed yourself overrides whatever was there; if you clear it,
    it's left empty (and the next enrich_beatport.py can fill it in)."""
    conn = get_connection()
    conn.execute(
        "UPDATE tracks SET key = ?, key_source = ?, key_alt = NULL,"
        " key_needs_review = 0, key_verified = ?, key_strength = NULL WHERE id = ?",
        (key, "manual" if key is not None else None, int(key is not None), track_id),
    )
    if key is not None:
        record_key_source(conn, track_id, "manual", key)
    else:
        conn.execute(
            "DELETE FROM key_sources WHERE track_id = ? AND source = 'manual'",
            (track_id,),
        )
    conn.commit()
    conn.close()


def use_key_source(track_id, source):
    """Chooses one detected key and marks the user's choice as verified."""
    conn = get_connection()
    row = conn.execute(
        "SELECT key, strength FROM key_sources"
        " WHERE track_id = ? AND source = ? AND key IS NOT NULL",
        (track_id, source),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    conn.execute(
        "UPDATE tracks SET key = ?, key_source = ?, key_alt = NULL,"
        " key_needs_review = 0, key_verified = 1, key_strength = ? WHERE id = ?",
        (row["key"], source, row["strength"], track_id),
    )
    conn.commit()
    conn.close()
    return row["key"]


def confirm_key(track_id):
    """Confirms the currently selected automatic key."""
    conn = get_connection()
    conn.execute(
        "UPDATE tracks SET key_alt = NULL, key_needs_review = 0,"
        " key_verified = 1 WHERE id = ? AND key IS NOT NULL",
        (track_id,),
    )
    conn.commit()
    conn.close()


def confirm_bpm(track_id):
    """The user reviewed the track and the saved BPM is fine: it becomes
    validated (the source stays as it was)."""
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
        pass  # no noise in the terminal

    def respond(self, body, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if self.path == "/":
            self.respond(PAGE, "text/html")
        elif self.path == "/api/data":
            self.respond(json.dumps(read_data()))
        elif re.fullmatch(r"/covers/\d+\.jpg", self.path):
            # Serves the covers downloaded by enrich_spotify.py. The pattern
            # only accepts "/covers/<number>.jpg", so there's no risk of
            # someone requesting other files on the machine.
            file = COVERS_DIR / Path(self.path).name
            if file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(file.read_bytes())
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in (
            "/api/bpm",
            "/api/confirm",
            "/api/key",
            "/api/source",
            "/api/key-source",
            "/api/key-confirm",
        ):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length))
            track_id = int(request["id"])
        except (ValueError, KeyError, json.JSONDecodeError):
            self.send_error(400)
            return

        if self.path == "/api/source":
            # Adopt a source's BPM (and validate it, because you chose it
            # looking at all the options).
            source = str(request.get("source") or "")
            bpm = use_source(track_id, source)
            if bpm is None:
                self.send_error(400)  # that source has no BPM for this track
                return
            self.respond(json.dumps({"ok": True, "bpm": bpm}))
            return

        if self.path == "/api/key-source":
            source = str(request.get("source") or "")
            key = use_key_source(track_id, source)
            if key is None:
                self.send_error(400)
                return
            self.respond(
                json.dumps({"ok": True, "key": key, "camelot": to_camelot(key)})
            )
            return

        if self.path == "/api/key-confirm":
            confirm_key(track_id)
            self.respond(json.dumps({"ok": True}))
            return

        if self.path == "/api/key":
            # Accepts "8A", "Am", "f# minor"...; empty = clear it.
            text = str(request.get("key") or "").strip()
            key = normalize_key(text) if text else None
            if text and key is None:
                self.send_error(400)  # the key wasn't understood
                return
            save_key(track_id, key)
            self.respond(json.dumps({"ok": True, "key": key, "camelot": to_camelot(key)}))
            return

        if self.path == "/api/confirm":
            confirm_bpm(track_id)
        else:
            try:
                bpm = request.get("bpm")
                if bpm is not None:
                    bpm = float(bpm)
                    if not 30 <= bpm <= 300:
                        raise ValueError(bpm)
            except ValueError:
                self.send_error(400)
                return
            save_bpm(track_id, bpm)
        self.respond(json.dumps({"ok": True}))


def main():
    init_db()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(
            f"Port {PORT} is already in use: the editor is probably already\n"
            f"open in another terminal (or was left running from before).\n"
            f"Go to http://localhost:{PORT} — and if you just updated the\n"
            f"project, close that one with Ctrl+C and run this again."
        )
        return
    url = f"http://localhost:{PORT}"
    print(f"BPM & key editor open at {url}")
    print("(if it didn't open on its own, go to that address in your browser)")
    print("To stop: Ctrl+C\n")
    threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone. All changes were already saved.")


if __name__ == "__main__":
    main()
