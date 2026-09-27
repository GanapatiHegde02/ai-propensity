#!/usr/bin/env python3
"""
AI Propensity Signal Collector — Claude.ai (web) Adapter

** STATUS: v0, unverified against a real export. ** Unlike the Claude Code
adapter (built and tested against real ~/.claude/projects transcripts), this
parser's field names are based on the publicly documented shape of Claude.ai's
personal data export and have NOT been run against an actual export file. Try
`import` on a real export and fix field names before relying on this for
anything — open the export JSON and compare its shape against `_messages_of()`
below.

Why an export parser and not a browser extension or live hook: Claude.ai is
a server-hosted web app with no local hook surface a third party can attach
to. The user's own data export (Settings -> Account -> Export data) is the
only channel that's both official/ToS-clean and rich enough to reconstruct
session/turn-level evidence — at the cost of being manual/periodic rather
than live, and never carrying real token counts (the export contains
message text, not API usage numbers, so token/cost fields are correctly
absent from every event this adapter writes).

Usage:
    python3 collect.py import <path-to-export.json-or-directory>
    python3 collect.py summary
    python3 collect.py classify [N]
    python3 collect.py export
"""

import datetime
import json
import sys
import zipfile
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
ROOT_DIR = ADAPTER_DIR.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core import event_schema, local_storage, retention, export as core_export, summary, llm_classify  # noqa: E402

SOURCE = "claude_web"

# Candidate filenames inside an unzipped/zipped Claude.ai data export.
CANDIDATE_FILENAMES = ["conversations.json", "conversations_v2.json"]


def _load_export(path_str):
    """Return the parsed conversations list, or None (with an explanation
    printed) if the shape isn't recognized. Never guesses silently."""
    path = Path(path_str)
    if not path.exists():
        print(f"Path not found: {path}")
        return None

    raw = None
    if path.is_file() and path.suffix == ".json":
        raw = json.loads(path.read_text())
    elif path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if Path(n).name in CANDIDATE_FILENAMES]
            if not names:
                print(f"No conversations JSON found inside {path} (looked for {CANDIDATE_FILENAMES}).")
                return None
            raw = json.loads(zf.read(names[0]))
    elif path.is_dir():
        for name in CANDIDATE_FILENAMES:
            candidate = path / name
            if candidate.exists():
                raw = json.loads(candidate.read_text())
                break
        if raw is None:
            print(f"No conversations JSON found in {path} (looked for {CANDIDATE_FILENAMES}).")
            return None
    else:
        print(f"Don't know how to read {path} — expected a .json file, a .zip, or a directory containing one.")
        return None

    if not isinstance(raw, list):
        print("Expected the export's top level to be a list of conversations — got something else. "
              "This adapter is unverified against a real export; check the actual shape and fix _load_export().")
        return None
    return raw


def _messages_of(conversation):
    """Normalize one conversation's messages to a list of
    {role, text, ts} dicts. Tries the documented `chat_messages` shape;
    logs and skips anything that doesn't match rather than guessing."""
    out = []
    for m in conversation.get("chat_messages", []) or []:
        role = m.get("sender") or m.get("role") or ""
        text = m.get("text", "")
        if isinstance(text, list):  # some export variants nest content blocks
            text = " ".join(str(c.get("text", "")) for c in text if isinstance(c, dict))
        out.append({"role": role, "text": text or "", "ts": m.get("created_at") or m.get("timestamp") or ""})
    return out


def cmd_import():
    if len(sys.argv) < 3:
        print("Usage: collect.py import <path-to-export.json | .zip | directory>")
        return
    conversations = _load_export(sys.argv[2])
    if conversations is None:
        return

    total_sessions = 0
    skipped = 0
    total_events = 0

    for conv in conversations:
        session_id = conv.get("uuid") or conv.get("id") or ""
        if not session_id:
            continue
        if local_storage.already_backfilled(session_id):
            skipped += 1
            continue

        messages = _messages_of(conv)
        if not messages:
            continue

        user_turns = sum(1 for m in messages if m["role"] in ("human", "user"))
        ai_turns = sum(1 for m in messages if m["role"] == "assistant")
        start_ts = conv.get("created_at") or (messages[0]["ts"] if messages else None)
        end_ts = conv.get("updated_at") or (messages[-1]["ts"] if messages else None)

        events = [event_schema.make_event("session_start", session_id, SOURCE, {
            "project": "claude.ai",
        }, ts=start_ts, historical=True, observed_at=event_schema.utcnow())]

        # ai_turn events deliberately carry no token fields — the export
        # contains message text, never API-level usage numbers. Counting
        # turns is still valid raw evidence — Valuezen's scoring only
        # needs the turn count here, not token fields.
        for m in messages:
            if m["role"] == "assistant":
                events.append(event_schema.make_event("ai_turn", session_id, SOURCE, {},
                                                        ts=m["ts"] or start_ts, historical=True, observed_at=event_schema.utcnow()))

        # `edit_count` is deliberately absent here, not zero — whether
        # Claude.ai's `chat_messages` export shape exposes edit/regenerate
        # structure at all is unverified, unlike ChatGPT's `mapping`
        # tree branching, which the chatgpt adapter reads directly. Leaving
        # the field out lets Valuezen's scoring treat this honestly as
        # "no edit-tracking data" rather than claiming a false zero.
        events.append(event_schema.make_event("session_end", session_id, SOURCE, {
            "trigger": "export_import",
            "ai_turns": ai_turns,
            "user_turns": user_turns,
            "duration_seconds": event_schema.duration_seconds(start_ts, end_ts),
            "user_chars_total": sum(len(m["text"]) for m in messages if m["role"] in ("human", "user")),
            # No real token counts exist in this export (see module
            # docstring), so this is the closest honest substitute for a
            # "response substance" signal — never the text itself, just
            # its length. Valuezen's scoring uses it as a labeled proxy,
            # not a token count, when no real token fields are present.
            "ai_chars_total": sum(len(m["text"]) for m in messages if m["role"] == "assistant"),
        }, ts=end_ts or start_ts, historical=True, observed_at=event_schema.utcnow()))

        total_sessions += 1
        for ev in events:
            date_str = (ev.get("ts") or "")[:10] or datetime.date.today().isoformat()
            local_storage.append_event(ev, date_str)
            total_events += 1

    print("Import complete.")
    print(f"  Conversations imported : {total_sessions}")
    print(f"  Skipped (already seen) : {skipped}")
    print(f"  Events written         : {total_events}")
    print(f"  Store location         : {local_storage.STORE_DIR}")
    if total_sessions == 0 and skipped == 0:
        print()
        print("Zero conversations imported — this most likely means the export's real field")
        print("names don't match what this v0 adapter expects. Inspect the export JSON by hand")
        print("and fix _messages_of()/cmd_import() in this file before relying on it.")


# ---------------------------------------------------------------------------
# Classify — same opt-in advanced tier as every other adapter, via
# core/llm_classify.py. Excerpt is the user's own message text only.
# ---------------------------------------------------------------------------

CONSENT_NOTICE = """
This will send a short excerpt of your own messages from this Claude.ai
export (not the assistant's replies) to your local `claude` CLI so it can
label each conversation's domain/topics/outcome. Nothing is sent to
Valuezen by this command itself; scoring happens in Valuezen's backend,
from raw counts, not from this label.
""".strip()


def _excerpt_for(conv, max_chars=6000):
    parts = [m["text"].strip() for m in _messages_of(conv) if m["role"] in ("human", "user") and m["text"].strip()]
    return "\n".join(parts)[:max_chars]


def cmd_classify():
    if len(sys.argv) < 3:
        print("Usage: collect.py classify <path-to-export> [N]")
        return
    conversations = _load_export(sys.argv[2])
    if conversations is None:
        return

    if not llm_classify.claude_cli_available():
        print("`claude` CLI not found on PATH — classify needs it to run headless self-classification.")
        return
    if not llm_classify.get_consent():
        print(CONSENT_NOTICE)
        print()
        if input("Proceed and remember this choice? [y/N] ").strip().lower() != "y":
            print("Cancelled. No content was sent.")
            return
        llm_classify.record_consent()

    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    classified = skipped = 0
    for conv in conversations:
        if classified >= limit:
            break
        session_id = conv.get("uuid") or conv.get("id") or ""
        if not session_id or local_storage.already_classified(session_id):
            skipped += 1
            continue
        excerpt = _excerpt_for(conv)
        ev = llm_classify.classify_excerpt(excerpt, session_id, SOURCE)
        if ev is None:
            continue
        date_str = ev.get("ts", "")[:10] or datetime.date.today().isoformat()
        local_storage.append_event(ev, date_str)
        classified += 1
        print(f"  {session_id[:8]}…  domain={ev['domain']:<20} outcome={ev['outcome']:<10} topics={', '.join(ev['topics'])}")

    print()
    print(f"Classified {classified} session(s), skipped {skipped}.")


def cmd_export():
    days, only_sources = core_export.parse_export_args(sys.argv[2:])
    out_path, count = core_export.export_evidence(SOURCE, days=days, only_sources=only_sources)
    if not out_path:
        print("No evidence to export.")
        return
    if only_sources:
        print(f"Filtered to source(s): {', '.join(sorted(only_sources))}")
    print(f"Exported {count} event(s). File: {out_path}")


def cmd_sync():
    """import + export in one call — the one command to run after each new
    export download. Mirrors adapters/claude-code's `sync`."""
    if len(sys.argv) < 3:
        print("Usage: collect.py sync <path-to-export.json | .zip | directory>")
        return
    cmd_import()
    print()
    out_path, count = core_export.export_evidence(SOURCE)
    if not out_path:
        print("Nothing to export yet.")
        return
    print()
    print(f"Ready to upload: {out_path}  ({count} events)")


def cmd_prune():
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None
    removed, days_used = retention.prune(days)
    print(f"Pruned {len(removed)} file(s) older than {days_used} days." if removed
          else f"Nothing to prune — no files older than {days_used} days.")


COMMANDS = {
    "import":   cmd_import,
    "sync":     cmd_sync,
    "classify": cmd_classify,
    "summary":  lambda: summary.print_summary(SOURCE),
    "export":   cmd_export,
    "prune":    cmd_prune,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd not in COMMANDS:
        print(f"Usage: collect.py [{' | '.join(COMMANDS)}]", file=sys.stderr)
        sys.exit(1)
    COMMANDS[cmd]()
