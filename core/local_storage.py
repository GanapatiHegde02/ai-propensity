"""The local, timestamped JSONL evidence store. Shared by every adapter —
nothing here is Claude-Code-specific.

Layout:
    ~/.valuezen/propensity/events/2026-09-16.jsonl
    ~/.valuezen/propensity/.state/<session_id>.json   (transient, hook-only)
"""

import json
import datetime
from pathlib import Path

try:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)
except ImportError:
    # Windows has no fcntl. msvcrt.locking locks a byte range rather than
    # the whole file, but locking one fixed byte at offset 0 is enough to
    # serialize this store's own single-writer-at-a-time appends/rewrites.
    import msvcrt

    def _lock(f):
        pos = f.tell()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        f.seek(pos)

    def _unlock(f):
        pos = f.tell()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.seek(pos)

STORE_DIR = Path.home() / ".valuezen" / "propensity" / "events"
STATE_DIR = Path.home() / ".valuezen" / "propensity" / ".state"


def day_file(date_str=None):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    day = date_str or datetime.date.today().isoformat()
    return STORE_DIR / f"{day}.jsonl"


def append_event(event, date_str=None):
    path = day_file(date_str)
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with open(path, "a") as f:
        _lock(f)
        f.write(line)
        _unlock(f)


def _has_event(session_id, event_type):
    for fpath in sorted(STORE_DIR.glob("*.jsonl")):
        with open(fpath) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    if ev.get("session_id") == session_id and ev.get("event") == event_type:
                        return True
                except Exception:
                    continue
    return False


BACKFILL_MANIFEST = STATE_DIR / "_backfill_manifest.json"


def _read_backfill_manifest():
    try:
        return json.loads(BACKFILL_MANIFEST.read_text()) if BACKFILL_MANIFEST.exists() else {}
    except Exception:
        return {}


def _write_backfill_manifest(manifest):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKFILL_MANIFEST.write_text(json.dumps(manifest))


def backfill_mtime(session_id):
    """Source-file mtime recorded the last time this session was backfilled,
    or None if it's never been processed. Compared against the transcript's
    current mtime so a still-growing session (e.g. one you're still in) gets
    re-processed on the next `setup`/`sync` instead of being skipped
    forever — see already_backfilled()'s old session_start-only check,
    which couldn't tell "done" from "captured once, kept growing"."""
    return _read_backfill_manifest().get(session_id)


def set_backfill_mtime(session_id, mtime):
    manifest = _read_backfill_manifest()
    manifest[session_id] = mtime
    _write_backfill_manifest(manifest)


def remove_events_where(predicate):
    """Delete every stored event (across all day files) for which
    `predicate(event)` is true, in a single pass over the store. Returns the
    count removed. Rewrites each touched day file under the same lock
    append_event uses."""
    removed = 0
    if not STORE_DIR.exists():
        return 0
    for fpath in sorted(STORE_DIR.glob("*.jsonl")):
        kept = []
        changed = False
        with open(fpath) as f:
            _lock(f)
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    kept.append(line)
                    continue
                if predicate(ev):
                    removed += 1
                    changed = True
                else:
                    kept.append(line)
            _unlock(f)
        if changed:
            with open(fpath, "w") as f:
                _lock(f)
                f.writelines(kept)
                _unlock(f)
    return removed


def remove_session_events(session_id):
    """Delete every stored event for this session so it can be re-backfilled
    from scratch without duplicating the events already captured from an
    earlier, incomplete pass. Returns the count removed."""
    return remove_events_where(lambda ev: ev.get("session_id") == session_id)


def remove_sessions_events(session_ids):
    """Batch form of remove_session_events — one pass for many sessions."""
    ids = set(session_ids)
    if not ids:
        return 0
    return remove_events_where(lambda ev: ev.get("session_id") in ids)


# Each adapter stamps the version of its parser into the manifest. When a
# parser fix changes what gets extracted from the same source file (e.g.
# de-duplicating Claude Code responses), every event that adapter wrote
# with the old parser is wrong, not just stale — so a version mismatch
# removes that source's events once and re-parses everything, instead of
# leaving old and new numbers side by side.
_PARSER_VERSIONS_KEY = "__parser_versions__"


def parser_version(source):
    return (_read_backfill_manifest().get(_PARSER_VERSIONS_KEY) or {}).get(source)


def set_parser_version(source, version):
    manifest = _read_backfill_manifest()
    versions = manifest.get(_PARSER_VERSIONS_KEY) or {}
    versions[source] = version
    manifest[_PARSER_VERSIONS_KEY] = versions
    _write_backfill_manifest(manifest)


def reset_source_if_parser_changed(source, version):
    """Returns True (after removing every stored event for `source`) when
    the stored parser version differs from `version`; the caller should then
    ignore recorded mtimes and re-parse everything. Returns False when the
    store is already current."""
    if parser_version(source) == version:
        return False
    remove_events_where(lambda ev: ev.get("source") == source)
    return True


def already_backfilled(session_id):
    """Return True if this session already has a session_start event stored.
    Kept for `already_classified`-style callers that only care about
    presence, not freshness — `setup`/`sync` use the mtime-aware
    backfill_mtime()/remove_session_events() pair above instead, so a
    session that's still growing gets refreshed rather than skipped."""
    return _has_event(session_id, "session_start")


def already_classified(session_id):
    """Return True if this session already has an (advanced-tier, opt-in)
    session_reflect event stored — so `classify` never re-spends tokens on
    the same session."""
    return _has_event(session_id, "session_reflect")


# ---------------------------------------------------------------------------
# Session state — lightweight temp files an adapter's live hooks can use to
# carry context (e.g. start time, running error counts) between hook firings.
# ---------------------------------------------------------------------------

def state_path(session_id):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{session_id}.json"


def read_state(session_id):
    p = state_path(session_id)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def write_state(session_id, data):
    state_path(session_id).write_text(json.dumps(data))


def delete_state(session_id):
    try:
        state_path(session_id).unlink()
    except Exception:
        pass
