#!/usr/bin/env python3
"""
AI Propensity Signal Collector — Claude.ai (web) Adapter

Verified against a real export (2026-10-05, 91 conversations): turn counts
and character totals match the export (conversations with no messages are
skipped). Conversations aren't linked to Projects in the export, so
Project use is read from the project_knowledge_search tool instead.

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
              "The export format may have changed; check the actual shape and fix _load_export().")
        return None
    return raw


# Parser history — bump when _features_of()/cmd_import() change what's
# extracted, so the next import replaces older claude_web evidence instead
# of skipping conversations it has already seen.
#   2 (2026-10-05): per-conversation `features`; conversations that changed
#     since the last import are replaced rather than skipped.
PARSER_VERSION = 2

_WEB_TOOLS = ("web_search", "web_search_fast", "web_fetch")
_CREATE_TOOLS = ("create_file", "artifacts", "repl", "present_files")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")
# Claude.ai's built-in connectors. Tool names for others vary, so this is a
# known-names list, not a guess from the name's shape.
_CONNECTOR_PREFIXES = (
    "google_drive", "gdrive", "gmail", "gcal", "google_calendar", "slack", "notion",
    "github", "asana", "linear", "jira", "confluence", "atlassian",
)


def _features_of(conversation):
    """Which optional Claude.ai capabilities this conversation used —
    presence only, read from tool names, attachment metadata and block
    types, never message text. Keys: web_search, file_upload, image_input,
    reasoning, deep_research, projects, connectors, created_files."""
    features = set()
    for m in conversation.get("chat_messages", []) or []:
        if m.get("attachments"):
            features.add("file_upload")
        for f in m.get("files") or []:
            name = ((f or {}).get("file_name") or "").lower()
            features.add("image_input" if name.endswith(_IMAGE_EXTS) else "file_upload")
        for block in m.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "thinking":
                features.add("reasoning")
            if kind != "tool_use":
                continue
            name = (block.get("name") or "").lower()
            if name in _WEB_TOOLS:
                features.add("web_search")
            elif name in _CREATE_TOOLS:
                features.add("created_files")
            elif name == "project_knowledge_search":
                features.add("projects")
            elif "research" in name:
                features.add("deep_research")
            elif name.startswith(_CONNECTOR_PREFIXES):
                features.add("connectors")
    return features


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
    refreshed = 0
    skipped = 0
    total_events = 0

    reparse_all = local_storage.reset_source_if_parser_changed(SOURCE, PARSER_VERSION)
    if reparse_all:
        print(f"Collector updated (parser v{PARSER_VERSION}) — replacing previously imported Claude.ai evidence.")

    pending = []  # (session_id, version, events)
    to_refresh = set()
    for conv in conversations:
        session_id = conv.get("uuid") or conv.get("id") or ""
        if not session_id:
            continue
        # A conversation that gained messages since the last import has a
        # newer updated_at — replace it rather than skipping it as seen.
        version = str(conv.get("updated_at") or "")
        recorded = None if reparse_all else local_storage.backfill_mtime(session_id)
        if recorded is not None and recorded == version:
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
        # turns is still valid raw evidence — AI Interaction only
        # needs the turn count here, not token fields.
        for m in messages:
            if m["role"] == "assistant":
                events.append(event_schema.make_event("ai_turn", session_id, SOURCE, {},
                                                        ts=m["ts"] or start_ts, historical=True, observed_at=event_schema.utcnow()))

        events.append(event_schema.make_event("session_end", session_id, SOURCE, {
            "trigger": "export_import",
            "ai_turns": ai_turns,
            "user_turns": user_turns,
            "duration_seconds": event_schema.duration_seconds(start_ts, end_ts),
            "user_chars_total": sum(len(m["text"]) for m in messages if m["role"] in ("human", "user")),
            # No real token counts exist in this export (see module
            # docstring). Lengths only, never the text: Valuezen uses
            # user + AI characters ÷ 4 for an *estimated* Token Usage
            # figure that's shown on the report but not scored.
            "ai_chars_total": sum(len(m["text"]) for m in messages if m["role"] == "assistant"),
            "features": sorted(_features_of(conv)),
        }, ts=end_ts or start_ts, historical=True, observed_at=event_schema.utcnow()))

        if recorded is not None:
            to_refresh.add(session_id)
            refreshed += 1
        else:
            total_sessions += 1
        pending.append((session_id, version, events))

    local_storage.remove_sessions_events(to_refresh)
    for session_id, version, events in pending:
        for ev in events:
            date_str = (ev.get("ts") or "")[:10] or datetime.date.today().isoformat()
            local_storage.append_event(ev, date_str)
            total_events += 1
        local_storage.set_backfill_mtime(session_id, version)
    local_storage.set_parser_version(SOURCE, PARSER_VERSION)

    print("Import complete.")
    print(f"  Conversations imported : {total_sessions}")
    print(f"  Updated since last time: {refreshed}")
    print(f"  Skipped (unchanged)    : {skipped}")
    print(f"  Events written         : {total_events}")
    print(f"  Store location         : {local_storage.STORE_DIR}")
    if total_sessions == 0 and skipped == 0 and refreshed == 0:
        print()
        print("Zero conversations imported — this most likely means the export's real field")
        print("names no longer match what this adapter expects. Inspect the export JSON by hand")
        print("and fix _messages_of()/cmd_import() in this file before relying on it.")


# ---------------------------------------------------------------------------
# Classify — deferred. Implementation kept for a future release, but not
# wired into COMMANDS below, so it is not reachable from the CLI.
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
