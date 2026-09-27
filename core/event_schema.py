"""Common, versioned event schema shared by every platform adapter.

Any adapter (claude-code, codex, vscode, ...) normalizes what it observes
into events built with make_event() so all evidence — regardless of source —
lands in the same shape on disk.
"""

import datetime

SCHEMA_VERSION = 1


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_seconds(ts_str):
    """Parse ISO8601 UTC string to unix timestamp, return None on failure."""
    if not ts_str:
        return None
    try:
        dt = datetime.datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        return None


def duration_seconds(start_ts, end_ts):
    """Return integer seconds between two ISO8601 strings, or None."""
    s = ts_to_seconds(start_ts)
    e = ts_to_seconds(end_ts)
    if s and e and e >= s:
        return e - s
    return None


def make_event(event_type, session_id, source, extra=None, ts=None, historical=False, observed_at=None):
    e = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts or utcnow(),
        "event": event_type,
        "source": source,
        "session_id": session_id,
    }
    if historical:
        e["historical"] = True
        e["observed_at"] = observed_at or utcnow()
        e["provenance"] = "local_history"
    if extra:
        e.update(extra)
    return e
