"""
download_music.py — Download a digital copy of your collection from Soulseek.

Reuses the same database the labels are built from (vinyl_labels.db): for each
record you own on vinyl, it looks the music up on Soulseek and downloads it,
preferring lossless. The result is a tidy, DJ-ready library on disk:

    ~/Music/Vinyl/<Artist> - <Album> (<CATNO>)/<position> <Title>.<ext>

with tags and the record cover embedded in every file.

How it finds the music
----------------------
Soulseek is a peer-to-peer network: other people share folders of files. The
heavy lifting (logging in, searching, queueing, retrying) is done by a small
background program called *slskd*, which you run once and leave open; this
script just tells it what to look for and where to put the results. See the
README section "Download digital copies (Soulseek)" to set it up.

For each record it searches Soulseek for every track on it individually — by
the track's own artist and title — matches results to your track list with a
fuzzy title compare, and queues the best-format copy of each with slskd, all
filed into one folder named after the record. Searching per track (rather than
for the album by name) matches how this music is shared: as loose singles, EP
cuts and DJ-collection files, seldom as a folder named after the release.

Downloading itself is slskd's job: this script queues transfers and checks in
on them every few seconds. Sitting in someone's upload queue is normal on
Soulseek — sometimes for a long while — so a queued or half-done transfer is
NEVER cancelled. Only when a source actually fails (rejects us, errors out,
disappears) is the track retried from the next person sharing it. Whatever is
still queued when the run ends stays queued inside slskd, and the next run
picks up the finished files before searching for anything else.

Format preference (best first): AIFF > FLAC > WAV > MP3 320. Whatever is found
is kept as-is — no conversion (rekordbox/Serato read all four).

How to run it
-------------
    python -m vinyl_labels download                # everything still missing
    python -m vinyl_labels download aphex          # filter by record
    python -m vinyl_labels download --force        # re-download existing
    python -m vinyl_labels download --retry-failed # retry recent failures
    python -m vinyl_labels download --deep         # wider search
    python -m vinyl_labels download --parallel 12  # concurrent records

Tracks that come up empty are remembered for a week and skipped on later runs,
so a repeat run is quick and only chases what might actually have appeared since.
Pass --retry-failed to search them again anyway.

Searching and downloading run side by side: worker threads search the records
and queue what they find, while the monitor files each finished track into the
library the moment slskd is done with it.

You can stop with Ctrl+C anytime: finished tracks are already saved, queued
transfers keep downloading inside slskd, and the next run collects them.
Records already fully downloaded are skipped without touching the network.
"""

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import slskd_api

from vinyl_labels import config
from vinyl_labels import soulseek as soulseek_helpers
from vinyl_labels.common import parse_duration
from vinyl_labels.db import get_connection, init_db

# Compatibility aliases for callers that imported these helpers from the old
# monolithic command. New code should import them from vinyl_labels.soulseek.
LEADING_INDEX = soulseek_helpers.LEADING_INDEX
artist_names = soulseek_helpers.artist_names
candidate_score = soulseek_helpers.candidate_score
clean_query = soulseek_helpers.clean_query
cover_bytes = soulseek_helpers.cover_bytes
format_rank = soulseek_helpers.format_rank
library_path = soulseek_helpers.library_path
locate_download = soulseek_helpers.locate_download
mentions = soulseek_helpers.mentions
place_and_tag = soulseek_helpers.place_and_tag
rank_candidates = soulseek_helpers.rank_candidates
remote_basename = soulseek_helpers.remote_basename
safe_name = soulseek_helpers.safe_name
tag_file = soulseek_helpers.tag_file
track_stem = soulseek_helpers.track_stem

# Set when you press Ctrl+C: every wait loop checks it, so all the worker
# threads wind down within a few seconds instead of finishing their record.
STOP = threading.Event()

# Set from --deep in main(). Off by default: each track is searched only as
# "<artist> <title>". On, we also fall back to a title-only search — that
# rarely finds anything the artist search missed, yet roughly doubles the
# number of searches, so it's opt-in. (A track with no credited artist always
# falls back to title-only regardless, or it could never be found at all.)
DEEP_SEARCH = False

# =========================================================
# Talking to slskd (search + queueing downloads)
# =========================================================
# slskd rate-limits how fast searches may be created (429 Too Many Requests)
# — and rapid-fire searches are bad manners on the Soulseek network anyway.
# With several records searched at once, all searches therefore go through
# this gate: one submission every SEARCH_GAP_S seconds, program-wide. One
# second is about as fast as slskd tolerates; the 429 backoff below absorbs
# the occasional overshoot.
SEARCH_GAP_S = 1
_search_gate = threading.Lock()
_last_search = [0.0]  # last submission time; guarded by _search_gate


def start_search(client, query, timeout_ms):
    """Submits one search, spaced out program-wide, retrying when slskd refuses
    it. Returns slskd's search state, or None if we're stopping or slskd kept
    refusing — in which case we skip THIS search rather than raising, so one
    unlucky query can't take down the whole record."""
    last_error = ""
    for attempt in range(1, 6):
        with _search_gate:
            wait = _last_search[0] + SEARCH_GAP_S - time.time()
            if wait > 0 and STOP.wait(wait):
                return None
            _last_search[0] = time.time()
            try:
                return client.searches.search_text(
                    searchText=query,
                    filterResponses=True,
                    searchTimeout=timeout_ms,
                )
            except Exception as e:
                last_error = str(e)
                # 429 (rate-limited) is worth several tries with a growing
                # pause. 400 (slskd rejected the query) and 409 (an identical
                # search is still active) rarely clear, so retry just once.
                # Anything else is a real fault worth surfacing.
                if not any(code in last_error for code in ("400", "409", "429")):
                    raise
                if attempt >= (5 if "429" in last_error else 2):
                    break
        # Back off outside the gate so other threads can proceed.
        if STOP.wait(5 * attempt):
            return None
    print(f"  ! slskd wouldn't run the search {query!r} ({last_error}); skipping it")
    return None


def run_search(client, query, timeout_ms=8000):
    """Runs one Soulseek search and returns its responses (one per person).
    Waits until slskd marks the search finished, then cleans it up."""
    state = start_search(client, query, timeout_ms)
    if state is None:
        return []
    search_id = state["id"]
    deadline = time.time() + timeout_ms / 1000 + 10
    complete = False
    while time.time() < deadline and not STOP.wait(1):
        try:
            if client.searches.state(search_id).get("isComplete"):
                complete = True
                break
        except Exception:  # momentary API hiccup; the deadline still applies
            continue
    try:
        return client.searches.search_responses(search_id)
    except Exception:
        return []
    finally:
        # Only clean up a search slskd is done with. Deleting one still in
        # flight (we bailed on the deadline or Ctrl+C) yanks its database row
        # out from under slskd, which then logs a concurrency error every time
        # it tries to update it; leftovers fall to slskd's own retention.
        if complete:
            try:
                client.searches.delete(search_id)
            except Exception:
                pass


def enqueue(client, username, files):
    """Asks slskd to queue a download from one person; True when accepted.
    From here on the transfer is slskd's business — the peer's upload queue,
    the retries, the actual bytes. The monitor below just checks in on it."""
    try:
        return bool(client.transfers.enqueue(username, files))
    except Exception:
        return False


# =========================================================
# Matching Soulseek results to your tracks
# =========================================================
def track_candidates(client, release, track, limit=3):
    """Per-track fallback: search '<artist> <title>' and return
    (candidates, saw_unconfirmed) — candidates as up to `limit`
    (file, username) pairs worth trying, best first and at most one per person,
    so when someone's queue goes nowhere or a transfer fails, we can go ask
    someone else. 'Best' prefers format, then closeness to the Discogs
    duration. saw_unconfirmed is True when same-titled files were rejected
    only because nothing in their path names the artist (may be the track,
    may not)."""
    credited = artist_names(track["artist"], release["artist"])
    target = parse_duration(track["duration_display"])
    unconfirmed = False
    title = clean_query(track["title"])
    if not title:
        return [], unconfirmed
    # Every credited artist gets a turn — on split records and collabs, files
    # are usually named after just one of them. The last resort is title-only,
    # in case nobody spells the artist the same way — but then the file's path
    # must at least mention the artist, or a SAME-TITLED track by someone else
    # entirely would match.
    queries = []
    for credit in credited[:3]:
        name = clean_query(credit) or credit  # symbols-only name: send it raw
        queries.append((f"{name} {title}", False))
    # Title-only fallback: only under --deep, or when there's no artist to
    # search by (otherwise this track would have no query at all). When we do
    # add it, the file's path must at least mention the artist (need_artist),
    # or a same-titled track by someone else entirely would match.
    if DEEP_SEARCH or not credited:
        queries.append((title, bool(credited)))

    best_per_user = {}  # username -> (key, file)
    for query, need_artist in queries:
        ranked_query, saw_unconfirmed = rank_candidates(
            run_search(client, query),
            track["title"],
            credited,
            target,
            need_artist,
        )
        unconfirmed = unconfirmed or saw_unconfirmed
        for file_info, username, score in ranked_query:
            if username not in best_per_user or score > best_per_user[username][0]:
                best_per_user[username] = (score, file_info)
        if best_per_user:
            break  # good enough from the more specific query; don't widen
    ranked = sorted(best_per_user.items(), key=lambda item: item[1][0], reverse=True)
    return [(f, user) for user, (_, f) in ranked[:limit]], unconfirmed


# =========================================================
# The board: every transfer we've queued, watched by one monitor
# =========================================================
# Downloading is slskd's job, not ours: we queue a transfer and check in on
# it every POLL_S seconds. Waiting in someone's upload queue is normal on
# Soulseek — sometimes for hours — so a queued or half-done transfer is never
# cancelled. Only when the SOURCE gives up (errored, rejected us, vanished)
# do we move on to the next person sharing the track. Whatever is still
# waiting when the run ends stays queued inside slskd; the row we keep in the
# pending_downloads table lets the next run pick the transfer back up.
POLL_S = 10              # seconds between check-ins with slskd
STATUS_EVERY_S = 60      # seconds between "still waiting on..." progress lines
QUIET_EXIT_S = 15 * 60   # searches done + NOTHING moved this long -> leave the queue to slskd
QUEUED_EXIT_S = 2 * 60   # searches done + everything merely waiting in remote queues,
                         # nothing actually downloading, this long -> same, but sooner:
                         # remote queues can take hours and slskd holds them after we exit
MISSING_GRACE_POLLS = 6  # check-ins a transfer may be absent from slskd before we react

UNCONFIRMED_NOTE = " (a same-titled file exists, but nothing says it's this artist — check by hand)"
GAVE_UP_NOTE = " (every source we tried failed — try again later)"


class Pending:
    """One track slskd is getting for us, plus every plan-B source to try."""

    def __init__(self, release, track, file, username, fallbacks,
                 searched_fallback=False, unconfirmed=False):
        self.release = release
        self.track = track
        self.file = file                  # the slskd file dict being downloaded
        self.username = username          # who we're downloading from
        self.fallbacks = list(fallbacks)  # (file, username) sources not yet tried
        self.searched_fallback = searched_fallback  # per-track search already done?
        self.unconfirmed = unconfirmed    # only same-titled, unverifiable copies seen
        self.state = ""                   # last state slskd reported
        self.bytes = 0                    # last byte count slskd reported
        self.missing_polls = 0            # check-ins the transfer wasn't in slskd's list
        self.locate_tries = 0             # times the finished file wasn't on disk yet


class Board:
    """What the search workers and the monitor share: the transfers in
    flight, the search work still running, and the running score."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}   # track_id -> Pending
        self.futures = []   # search work still running (records + rescues)
        self.downloaded = 0
        self.not_found = []          # lines for the final report
        self.touched = time.time()   # last time anything at all moved

    def has(self, track_id):
        with self.lock:
            return track_id in self.pending

    def add(self, conn, pending):
        """Puts a freshly queued transfer on the board, and in the database
        so an interrupted run can pick it back up."""
        remember_pending(conn, pending.track["id"], pending.username, pending.file["filename"])
        with self.lock:
            self.pending[pending.track["id"]] = pending
            self.touched = time.time()

    def report_missing(self, line):
        with self.lock:
            self.not_found.append(line)


def remember_pending(conn, track_id, username, filename):
    """Saves 'we asked `username` for `filename`' so a later run can pick the
    transfer back up (slskd keeps downloading after this script exits)."""
    conn.execute(
        "INSERT OR REPLACE INTO pending_downloads (track_id, username, filename) VALUES (?, ?, ?)",
        (track_id, username, filename),
    )
    conn.commit()


def forget_pending(conn, track_id):
    conn.execute("DELETE FROM pending_downloads WHERE track_id = ?", (track_id,))
    conn.commit()


def remember_failed(conn, track_id, reason):
    """Records that a track couldn't be downloaded now, with a timestamp, so
    later runs skip it for a week instead of re-searching the whole network for
    something that isn't out there. Cleared the moment the track does download."""
    conn.execute(
        "INSERT OR REPLACE INTO failed_downloads (track_id, reason, failed_at)"
        " VALUES (?, ?, datetime('now'))",
        (track_id, reason),
    )
    conn.commit()


def forget_failed(conn, track_id):
    conn.execute("DELETE FROM failed_downloads WHERE track_id = ?", (track_id,))
    conn.commit()


def recent_failures(conn, days=7):
    """Track ids that failed to download within the last `days`. Skipped by
    default so a run doesn't keep chasing tracks that aren't on Soulseek;
    --retry-failed (or --force) searches them anyway."""
    rows = conn.execute(
        "SELECT track_id FROM failed_downloads WHERE failed_at > datetime('now', ?)",
        (f"-{days} days",),
    ).fetchall()
    return {row["track_id"] for row in rows}


def all_transfers(client):
    """Everything slskd is downloading (or finished downloading), as a map of
    (username, remote path) -> transfer. None on a momentary API hiccup, so
    the caller can tell 'slskd didn't answer' from 'the transfer is gone'."""
    try:
        users = client.transfers.get_all_downloads()
    except Exception:
        return None
    transfers = {}
    for user in users or []:
        for directory in user.get("directories", []):
            for tf in directory.get("files", []):
                transfers[(user.get("username"), tf.get("filename"))] = tf
    return transfers


def short_state(state):
    """slskd's transfer state, for humans: 'Queued, Remotely' -> 'queued'."""
    state = state or ""
    if "InProgress" in state:
        return "downloading"
    if "Queued" in state:
        return "queued at the source"
    if "Initializing" in state or "Requested" in state:
        return "starting"
    if "Succeeded" in state:
        return "finished"
    if "Completed" in state:  # Errored / Cancelled / TimedOut / Rejected
        return state.split(",")[-1].strip().lower() or "failed"
    return state.lower() or "waiting"


def queue_source(client, candidates):
    """Asks slskd to queue the first source that accepts. Returns
    (file, username, untried_rest) or None when every source refused."""
    candidates = list(candidates)
    while candidates and not STOP.is_set():
        f, username = candidates.pop(0)
        if enqueue(client, username, [f]):
            return f, username, candidates
    return None


def finalize(board, conn, p):
    """Files one finished download into the library: locate it on disk, move,
    tag, record in the database. True when the file was found and placed."""
    local = locate_download(p.file, p.track)
    if local is None:
        return False
    dest = place_and_tag(local, p.track, p.release)
    save_track_audio(conn, p.track["id"], dest, p.username)
    forget_pending(conn, p.track["id"])
    forget_failed(conn, p.track["id"])  # it downloaded after all; don't skip it next time
    with board.lock:
        board.pending.pop(p.track["id"], None)
        board.downloaded += 1
        board.touched = time.time()
    prefix = f"{p.track['position']} " if p.track["position"] else ""
    print(f"  + {p.release['artist']} - {p.release['title']}: {prefix}{p.track['title']}"
          f" ({dest.suffix.lstrip('.')}, from {p.username})")
    return True


def resolve_missing(board, conn, p, note):
    """Takes a track off the board for good and records it for the report."""
    forget_pending(conn, p.track["id"])
    remember_failed(conn, p.track["id"], note.strip() or "every source failed")
    line = f"{p.release['artist']} - {p.track['title']}{note}"
    with board.lock:
        board.pending.pop(p.track["id"], None)
        board.not_found.append(line)
        board.touched = time.time()
    print(f"  ! {line}")


def give_up_source(board, pool, conn, client, p, why):
    """The current source failed for good (their side errored, rejected us,
    or the transfer vanished). Move to the next source we already know about;
    with none left, search once more for this one track; only when that too
    comes up empty is the track reported as not downloadable."""
    if STOP.is_set():
        return  # leave it on the board; the database row lets the next run retry
    label = f"{p.release['artist']} - {p.track['title']}"
    queued = queue_source(client, p.fallbacks)
    if queued:
        print(f"  ! {label}: {why} from {p.username}; trying the next source")
        p.file, p.username, p.fallbacks = queued
        p.state, p.bytes = "", 0
        p.missing_polls = p.locate_tries = 0
        remember_pending(conn, p.track["id"], p.username, p.file["filename"])
        with board.lock:
            board.touched = time.time()
        return
    if not p.searched_fallback:
        print(f"  ! {label}: {why} from {p.username}; searching for another source")
        forget_pending(conn, p.track["id"])
        with board.lock:
            board.pending.pop(p.track["id"], None)
            board.futures.append(pool.submit(rescue_track, board, p.release, p.track))
            board.touched = time.time()
        return
    resolve_missing(board, conn, p, GAVE_UP_NOTE)


def check_transfer(board, pool, conn, client, p, tf):
    """Looks at what slskd says about one of our transfers and reacts.
    Queued and in-progress transfers are simply left to keep going."""
    if tf is None:
        # Not in slskd's list (yet, or anymore). Give it a few check-ins:
        # right after queueing, a transfer can take a moment to show up.
        p.missing_polls += 1
        if p.missing_polls <= MISSING_GRACE_POLLS:
            return
        # Gone for good — unless it finished and the list was cleared.
        if not finalize(board, conn, p):
            give_up_source(board, pool, conn, client, p, "transfer vanished")
        return
    p.missing_polls = 0
    state = tf.get("state", "")
    transferred = tf.get("bytesTransferred") or 0
    if state != p.state or transferred != p.bytes:
        p.state, p.bytes = state, transferred
        with board.lock:
            board.touched = time.time()
    if "Succeeded" in state:
        if finalize(board, conn, p):
            return
        p.locate_tries += 1  # done per slskd; the file can take a moment to land
        if p.locate_tries > 3:
            resolve_missing(board, conn, p,
                            " (downloaded, but the file never appeared in the downloads folder)")
    elif "Completed" in state:  # Errored / Cancelled / TimedOut / Rejected
        give_up_source(board, pool, conn, client, p, short_state(state))
    # Anything else — queued, initializing, downloading — just keeps going.


def monitor(board, pool, conn, client):
    """The one loop that watches every queued transfer until the searches are
    done and each track is either filed, failed everywhere, or clearly going
    nowhere for now. Tracks still waiting in someone's upload queue at that
    point are LEFT QUEUED in slskd for a later run — never cancelled."""
    last_status = time.time()
    while not STOP.is_set():
        with board.lock:
            searching = any(not f.done() for f in board.futures)
            items = list(board.pending.values())
        if not searching and not items:
            return
        transfers = all_transfers(client)
        if transfers is not None:
            for p in items:
                if STOP.is_set():
                    return
                check_transfer(board, pool, conn, client, p,
                               transfers.get((p.username, p.file["filename"])))
        with board.lock:
            waiting = len(board.pending)
            idle = time.time() - board.touched
            # Everything left is just sitting in someone's remote upload queue —
            # nothing is actually downloading (which would be "downloading", or
            # would keep board.touched fresh as bytes arrive).
            all_queued = waiting > 0 and all(
                short_state(p.state) == "queued at the source"
                for p in board.pending.values()
            )
        if not searching and waiting and (
            idle > QUIET_EXIT_S or (all_queued and idle > QUEUED_EXIT_S)
        ):
            minutes = (QUEUED_EXIT_S if all_queued and idle <= QUIET_EXIT_S else QUIET_EXIT_S) // 60
            print(f"\nNothing has moved in {minutes} minutes; {waiting} tracks are still"
                  "\nwaiting in other people's upload queues. Leaving them queued in slskd —"
                  "\nrun this again later to pick up whatever finished.")
            return
        if waiting and time.time() - last_status >= STATUS_EVERY_S:
            counts = {}
            with board.lock:
                for p in board.pending.values():
                    what = short_state(p.state)
                    counts[what] = counts.get(what, 0) + 1
                done = board.downloaded
            status = ", ".join(f"{n} {what}" for what, n in sorted(counts.items(), key=lambda kv: -kv[1]))
            print(f"  ... {done} filed so far; waiting on {status}"
                  + ("; still searching" if searching else ""))
            last_status = time.time()
        if STOP.wait(POLL_S):
            return


# =========================================================
# Orchestration
# =========================================================
def has_audio(track):
    """True if this track already has a downloaded file that still exists."""
    path = track["audio_path"]
    return bool(path) and Path(path).exists()


def save_track_audio(conn, track_id, dest, username):
    conn.execute(
        "UPDATE tracks SET audio_path = ?, audio_format = ?, audio_source = ? WHERE id = ?",
        (str(dest), dest.suffix.lower().lstrip("."), username, track_id),
    )
    conn.commit()


def search_release(board, item, total, recently_failed):
    """Searches every track on one record individually and queues the best
    source for each with slskd, filing them all into the record's own folder.
    Runs in a worker thread and does NOT wait for any download — watching those
    is the monitor's job, so the next record's searches start right away."""
    i, release, tracks = item
    label = f"[{i}/{total}] {release['artist']} - {release['title']}"
    conn = get_connection()
    conn.execute("PRAGMA busy_timeout = 5000")  # if two threads save at once, wait, don't error
    try:
        client = make_client()
        remaining = [t for t in tracks
                     if not has_audio(t) and not board.has(t["id"])
                     and t["id"] not in recently_failed]
        if not remaining:
            return
        print(f"{label}: searching...")

        # One search per track — '<artist> <title>', best copy first and at
        # most one per person (see track_candidates), so a stalled queue can
        # later be swapped for someone else. Nothing here waits for bytes:
        # queue the best source and move on; the monitor files each file the
        # moment slskd is done fetching it.
        queued_count = 0
        for track in remaining:
            if STOP.is_set():
                return
            candidates, unconfirmed = track_candidates(client, release, track)
            queued = queue_source(client, candidates)
            if queued:
                f, username, rest = queued
                board.add(conn, Pending(release, track, f, username, rest,
                                        searched_fallback=True, unconfirmed=unconfirmed))
                queued_count += 1
            elif not STOP.is_set():
                note = UNCONFIRMED_NOTE if unconfirmed else ""
                board.report_missing(f"{release['artist']} - {track['title']}{note}")
                remember_failed(conn, track["id"], "nothing to download")
                print(f"  ! {release['artist']} - {track['title']}: nothing to download{note}")
        print(f"{label}: {queued_count} of {len(remaining)} tracks queued")
    except Exception as e:  # one bad record shouldn't take down the whole run
        print(f"{label}: failed ({e})")
    finally:
        conn.close()


def rescue_track(board, release, track):
    """Runs in the search pool when a track ran out of known sources: one
    more per-track search, then either a fresh transfer on the board or a
    final verdict for the report."""
    if STOP.is_set():
        return
    conn = get_connection()
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        client = make_client()
        candidates, unconfirmed = track_candidates(client, release, track)
        queued = queue_source(client, candidates)
        if queued:
            f, username, rest = queued
            board.add(conn, Pending(release, track, f, username, rest,
                                    searched_fallback=True, unconfirmed=unconfirmed))
        elif not STOP.is_set():
            note = UNCONFIRMED_NOTE if unconfirmed else GAVE_UP_NOTE
            board.report_missing(f"{release['artist']} - {track['title']}{note}")
            remember_failed(conn, track["id"], note.strip() or "every source failed")
            print(f"  ! {release['artist']} - {track['title']}{note}")
    except Exception as e:
        print(f"  ! {release['artist']} - {track['title']}: retry failed ({e})")
    finally:
        conn.close()


def adopt_previous_run(board, conn, client):
    """Transfers queued by an earlier run are still alive inside slskd — we
    never cancel anything. Pick up where that run left off: finished files
    are placed and tagged right now, live transfers go back on the board
    (their searches are skipped), and dead ones are forgotten so the normal
    search finds a new source."""
    rows = conn.execute("SELECT * FROM pending_downloads").fetchall()
    if not rows:
        return
    transfers = all_transfers(client) or {}
    filed, live = 0, 0
    for row in rows:
        track = conn.execute("SELECT * FROM tracks WHERE id = ?", (row["track_id"],)).fetchone()
        release = track and conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (track["release_id"],)
        ).fetchone()
        if track is None or release is None or has_audio(track):
            forget_pending(conn, row["track_id"])
            continue
        p = Pending(release, track, {"filename": row["filename"]}, row["username"], [])
        tf = transfers.get((row["username"], row["filename"]))
        if tf is not None and "Completed" not in tf.get("state", ""):
            with board.lock:  # still queued or downloading: watch it again
                board.pending[track["id"]] = p
            live += 1
        elif finalize(board, conn, p):  # finished while we were away
            filed += 1
        else:  # failed or vanished: let the normal search find a new source
            forget_pending(conn, row["track_id"])
    if filed or live:
        print(f"From the last run: {filed} finished downloads filed, {live} still queued in slskd.\n")


def make_client():
    """A fresh slskd client. Each worker thread gets its own, because the
    HTTP session underneath isn't safe to share between threads."""
    return slskd_api.SlskdClient(
        host=config.SLSKD_HOST,
        api_key=config.SLSKD_API_KEY,
        url_base=config.SLSKD_URL_BASE,
    )


def connect():
    """Connects to slskd and returns the client, or None with an explanation."""
    if not config.SLSKD_API_KEY:
        print("SLSKD_API_KEY is empty. Put slskd's API key in your .env file.")
        print('See the README section "Download digital copies (Soulseek)".')
        return None
    try:
        client = make_client()
        state = client.application.state()
    except Exception:
        print(f"Couldn't reach slskd at {config.SLSKD_HOST}.")
        print("Start it in another terminal with:  make slskd")
        return None
    server = (state.get("server") or {}).get("state", "")
    if "Connected" not in server:
        print("slskd is running but not logged in to Soulseek yet.")
        print("Check the Soulseek username/password in your slskd.yml, then retry.")
        return None
    return client


def main(arguments=None):
    global DEEP_SEARCH
    parser = argparse.ArgumentParser(
        prog="python -m vinyl_labels download",
        description="Download digital copies of owned records through slskd."
    )
    parser.add_argument("filter", nargs="?", help="artist/record text to match")
    parser.add_argument("--force", action="store_true", help="download existing tracks again")
    parser.add_argument("--retry-failed", action="store_true", help="retry recent misses")
    parser.add_argument("--deep", action="store_true", help="also run title-only searches")
    parser.add_argument("--parallel", type=int, default=8, metavar="N")
    args = parser.parse_args(arguments)
    if args.parallel < 1:
        parser.error("--parallel must be a positive integer")
    force = args.force
    retry_failed = args.retry_failed
    DEEP_SEARCH = args.deep
    parallel = args.parallel
    filter_text = (args.filter or "").lower()

    init_db()
    conn = get_connection()
    conn.execute("PRAGMA busy_timeout = 5000")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM releases ORDER BY artist, title")
    releases = cursor.fetchall()
    if not releases:
        print("Your collection is empty. Run first: python -m vinyl_labels fetch")
        conn.close()
        return 1

    if filter_text:
        releases = [r for r in releases if filter_text in f"{r['artist']} {r['title']}".lower()]
        if not releases:
            print(f"No record in your collection contains '{filter_text}'.")
            conn.close()
            return 1

    client = connect()
    if client is None:
        conn.close()
        return 2

    # Whatever a previous run left queued has kept downloading inside slskd:
    # collect the finished files and put the live transfers back on the board.
    board = Board()
    adopt_previous_run(board, conn, client)

    # Tracks that recently came up empty on Soulseek are skipped so a repeat run
    # stays quick — unless --force (redo everything) or --retry-failed (chase
    # them again anyway) overrides that.
    recently_failed = set() if (force or retry_failed) else recent_failures(conn)
    skipped_failed = 0

    print(f"Records to process: {len(releases)}")

    # First a quick pass with no network: decide what actually needs work.
    todo = []
    for i, release in enumerate(releases, start=1):
        cursor.execute(
            "SELECT * FROM tracks WHERE release_id = ? ORDER BY sort_order, id",
            (release["release_id"],),
        )
        tracks = cursor.fetchall()
        if not tracks:
            continue

        if force:
            # Re-download: forget existing files AND queued transfers, so
            # everything counts as missing again.
            cursor.execute(
                "UPDATE tracks SET audio_path = NULL WHERE release_id = ?",
                (release["release_id"],),
            )
            cursor.execute(
                "DELETE FROM pending_downloads WHERE track_id IN"
                " (SELECT id FROM tracks WHERE release_id = ?)",
                (release["release_id"],),
            )
            cursor.execute(
                "DELETE FROM failed_downloads WHERE track_id IN"
                " (SELECT id FROM tracks WHERE release_id = ?)",
                (release["release_id"],),
            )
            conn.commit()
            with board.lock:
                for t in tracks:
                    board.pending.pop(t["id"], None)
            tracks = cursor.execute(
                "SELECT * FROM tracks WHERE release_id = ? ORDER BY sort_order, id",
                (release["release_id"],),
            ).fetchall()
        elif all(has_audio(t) for t in tracks):
            print(f"[{i}/{len(releases)}] {release['artist']} - {release['title']}: already downloaded, skipping")
            continue
        elif all(has_audio(t) or board.has(t["id"]) for t in tracks):
            print(f"[{i}/{len(releases)}] {release['artist']} - {release['title']}: queued since the last run, watching it")
            continue

        # Of what's still missing, set aside tracks that came up empty recently.
        # If that's all of them, there's nothing to search on this record now.
        missing = [t for t in tracks if not has_audio(t) and not board.has(t["id"])]
        failed_here = [t for t in missing if t["id"] in recently_failed]
        skipped_failed += len(failed_here)
        if failed_here and len(failed_here) == len(missing):
            print(f"[{i}/{len(releases)}] {release['artist']} - {release['title']}: "
                  f"all {len(missing)} remaining track(s) failed recently, skipping (--retry-failed to try again)")
            continue

        todo.append((i, release, tracks))

    # Then the real work: worker threads search the records and queue what
    # they find with slskd, while the monitor (this thread) files finished
    # tracks and moves failed ones to their next source. Nothing queued is
    # ever cancelled — a source that keeps us waiting keeps its place.
    if todo or board.pending:
        print("(searches and downloads run side by side; stop with Ctrl+C anytime —\n"
              " queued transfers keep downloading inside slskd, and the next run\n"
              " collects whatever finished)\n")
        workers = min(parallel, len(todo)) if todo else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            with board.lock:
                board.futures = [pool.submit(search_release, board, item, len(releases), recently_failed)
                                 for item in todo]
            try:
                monitor(board, pool, conn, client)
            except KeyboardInterrupt:
                STOP.set()
                with board.lock:
                    for f in board.futures:
                        f.cancel()
                print("\nStopping... nothing is cancelled: queued transfers keep downloading"
                      "\ninside slskd, and the next run picks up whatever finished.")
        # (leaving the "with" waits for the searches that were mid-flight)

    with board.lock:
        downloaded = board.downloaded
        not_found = list(board.not_found)
        still_queued = sorted(
            board.pending.values(),
            key=lambda p: (p.release["artist"] or "", p.track["title"] or ""),
        )

    complete = 0
    for release in releases:
        tracks = conn.execute(
            "SELECT * FROM tracks WHERE release_id = ?", (release["release_id"],)
        ).fetchall()
        if tracks and all(has_audio(t) for t in tracks):
            complete += 1
    conn.close()

    print("\n" + "=" * 50)
    print(f"Downloaded {downloaded} tracks. Records complete: {complete}/{len(releases)}.")
    print(f"Your library: {Path(config.MUSIC_DIR).expanduser()}")
    if skipped_failed:
        print(f"Skipped {skipped_failed} track(s) that came up empty in the last week "
              "(--retry-failed to search them again).")
    if still_queued:
        print(f"\n{len(still_queued)} tracks are still queued in slskd and keep downloading on"
              "\ntheir own; run this again later to file them into the library:")
        for p in still_queued:
            print(f"  - {p.release['artist']} - {p.track['title']} ({short_state(p.state)}, from {p.username})")
    if not_found:
        print(f"\n{len(not_found)} tracks couldn't be downloaded "
              "(try them by hand on Soulseek, or buy them on Bandcamp):")
        for line in not_found:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
