#!/usr/bin/env python3
"""
AI Propensity Signal Collector — ChatGPT (web) Adapter

Verified against a real export (2026-10-05, ~500 conversations, split
into conversations-NNN.json files): turn counts and character totals match
the export exactly. The export keeps only the final branch of each
conversation, so edits/regenerations are invisible to it.

Same rationale as the claude-web adapter: ChatGPT is server-hosted with no
local hook surface, so the user's own data export (Settings -> Data
controls -> Export) is the practical, ToS-clean channel. No token/cost
fields are ever written here — the export has message text, not API usage
numbers.

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

SOURCE = "chatgpt"
CANDIDATE_FILENAMES = ["conversations.json"]
# Large accounts get `conversations-000.json`, `conversations-001.json`, ...
# instead of one `conversations.json` (confirmed against a real ~500-
# conversation export, 2026-09-26) — same top-level list shape, just chunked.
SPLIT_NAME_RE_PREFIX = "conversations-"


def _merge_conversation_lists(raw_by_name):
    """Concatenate every `conversations*.json` payload found, in filename
    order, so a split export behaves like one `conversations.json` list."""
    merged = []
    for name in sorted(raw_by_name):
        payload = raw_by_name[name]
        if not isinstance(payload, list):
            print(f"Expected {name} to be a list of conversations — got something else. Skipping it.")
            continue
        merged.extend(payload)
    return merged


def _load_export(path_str):
    path = Path(path_str)
    if not path.exists():
        print(f"Path not found: {path}")
        return None

    raw = None
    if path.is_file() and path.suffix == ".json":
        raw = json.loads(path.read_text())
    elif path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [
                n for n in zf.namelist()
                if Path(n).name in CANDIDATE_FILENAMES or Path(n).name.startswith(SPLIT_NAME_RE_PREFIX)
            ]
            if not names:
                print(f"No conversations.json (or conversations-NNN.json) found inside {path}.")
                return None
            raw = _merge_conversation_lists({n: json.loads(zf.read(n)) for n in names})
    elif path.is_dir():
        candidate = path / "conversations.json"
        split_files = sorted(path.glob("conversations-*.json"))
        if candidate.exists():
            raw = json.loads(candidate.read_text())
        elif split_files:
            raw = _merge_conversation_lists({f.name: json.loads(f.read_text()) for f in split_files})
        else:
            print(f"No conversations.json or conversations-NNN.json found in {path}.")
            return None
    else:
        print(f"Don't know how to read {path} — expected a .json file, a .zip, or a directory containing one.")
        return None

    if not isinstance(raw, list):
        print("Expected the export's top level to be a list of conversations — got something else. "
              "The export format may have changed; check the actual shape and fix _load_export().")
        return None
    return raw


def _messages_of(conversation):
    """Walk the `mapping` node tree in create_time order, normalizing to
    {role, text, ts, model} dicts. The tree-walk is a best-effort linear
    flatten (by timestamp), not a true branch-aware reconstruction — good
    enough for turn counts and content excerpts, not for exact conversation
    structure with edits/regenerations."""
    mapping = conversation.get("mapping", {})
    nodes = []
    for node in mapping.values():
        msg = node.get("message")
        if not msg:
            continue
        role = (msg.get("author") or {}).get("role", "")
        parts = (msg.get("content") or {}).get("parts", [])
        text = " ".join(str(p) for p in parts if isinstance(p, str))
        if not text.strip():
            continue
        nodes.append({
            "role": role,
            "text": text,
            "ts": msg.get("create_time"),
            "model": (msg.get("metadata") or {}).get("model_slug", ""),
        })
    nodes.sort(key=lambda n: n["ts"] or 0)
    return nodes


# Parser history — bump when _features_of()/cmd_import() change what's
# extracted, so the next import replaces older chatgpt evidence instead of
# skipping conversations it has already seen.
#   2 (2026-10-05): per-conversation `features`; `edit_count` dropped (the
#     export keeps only each chat's final branch, so edits were never
#     visible — it read 0 for every conversation); conversations that
#     changed since the last import are replaced rather than skipped.
PARSER_VERSION = 2

_WEB_REFERENCE_TYPES = ("grouped_webpages", "webpage_extended", "webpage")


def _features_of(conversation):
    """Which optional ChatGPT capabilities this conversation used —
    presence only, read from export metadata, never message text. Keys:
    web_search, file_upload, image_input, reasoning, deep_research,
    projects, connectors, created_files. Verified against a real export
    (2026-10-05); the tool-message checks cover fuller exports that keep
    tool messages, which that one didn't."""
    features = set()
    template = conversation.get("conversation_template_id") or conversation.get("gizmo_id") or ""
    # "g-p-..." is a Project. Other "g-..." ids aren't reliably custom GPTs
    # (one such id sat on 65% of a real user's ordinary chats), so they're
    # not counted.
    if isinstance(template, str) and template.startswith("g-p-"):
        features.add("projects")
    if conversation.get("plugin_ids"):
        features.add("connectors")

    for node in (conversation.get("mapping") or {}).values():
        msg = node.get("message")
        if not msg:
            continue
        md = msg.get("metadata") or {}
        content = msg.get("content") or {}
        author = msg.get("author") or {}
        role = author.get("role", "")
        tool_name = (author.get("name") or "") if role == "tool" else ""
        recipient = msg.get("recipient") or ""
        model = md.get("model_slug") or ""

        if md.get("search_result_groups") or any(
            (r or {}).get("type") in _WEB_REFERENCE_TYPES for r in (md.get("content_references") or [])
        ) or tool_name.startswith(("web", "browser")) or recipient.startswith(("web", "browser")):
            features.add("web_search")

        for a in md.get("attachments") or []:
            mime = (a or {}).get("mime_type") or ""
            features.add("image_input" if mime.startswith("image/") else "file_upload")
        if role == "user" and content.get("content_type") == "multimodal_text":
            if any(isinstance(part, dict) and part.get("content_type") == "image_asset_pointer"
                   for part in content.get("parts") or []):
                features.add("image_input")

        # ChatGPT sometimes routes to a reasoning model on its own; the
        # 2-session rule in scoring keeps one auto-switch from counting.
        if content.get("content_type") in ("thoughts", "reasoning_recap") or "thinking" in model:
            features.add("reasoning")
        if md.get("async_task_title") or "research" in model:
            features.add("deep_research")
        if tool_name.startswith(("canmore", "python", "dalle", "image_gen")) or recipient.startswith(("canmore", "python")):
            features.add("created_files")
    return features


def _user_chars_total(messages):
    """Length only, never the text itself — feeds the Prompt &
    Conversation Depth vector. Computed from the same `messages` list `_messages_of()` already
    builds for turn counting; the text is never written to disk or
    exported, only its length."""
    return sum(len(m["text"]) for m in messages if m["role"] == "user")


def _ai_chars_total(messages):
    """Same length-only treatment as `_user_chars_total`, for assistant
    replies. ChatGPT's export has no per-message usage numbers, so
    Valuezen estimates Token Usage as (user + AI characters) ÷ 4 — shown
    on the report as an estimate, never scored."""
    return sum(len(m["text"]) for m in messages if m["role"] == "assistant")


def _iso(unix_ts):
    if not unix_ts:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(unix_ts), tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


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
        print(f"Collector updated (parser v{PARSER_VERSION}) — replacing previously imported ChatGPT evidence.")

    pending = []  # (session_id, version, events)
    to_refresh = set()
    for conv in conversations:
        session_id = conv.get("conversation_id") or conv.get("id") or ""
        if not session_id:
            continue
        # A conversation that gained messages since the last import has a
        # newer update_time — replace it rather than skipping it as seen.
        version = str(conv.get("update_time") or "")
        recorded = None if reparse_all else local_storage.backfill_mtime(session_id)
        if recorded is not None and recorded == version:
            skipped += 1
            continue

        messages = _messages_of(conv)
        if not messages:
            continue

        user_turns = sum(1 for m in messages if m["role"] == "user")
        assistant_turns = [m for m in messages if m["role"] == "assistant"]
        start_ts = _iso(conv.get("create_time")) or messages[0]["ts"] and _iso(messages[0]["ts"])
        end_ts = _iso(conv.get("update_time")) or (messages[-1]["ts"] and _iso(messages[-1]["ts"]))

        events = [event_schema.make_event("session_start", session_id, SOURCE, {
            "project": "chatgpt.com",
        }, ts=start_ts, historical=True, observed_at=event_schema.utcnow())]

        for m in assistant_turns:
            events.append(event_schema.make_event("ai_turn", session_id, SOURCE, {
                "model": m.get("model", ""),
            }, ts=_iso(m["ts"]) or start_ts, historical=True, observed_at=event_schema.utcnow()))

        events.append(event_schema.make_event("session_end", session_id, SOURCE, {
            "trigger": "export_import",
            "ai_turns": len(assistant_turns),
            "user_turns": user_turns,
            "duration_seconds": event_schema.duration_seconds(start_ts, end_ts),
            "user_chars_total": _user_chars_total(messages),
            "ai_chars_total": _ai_chars_total(messages),
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
This will send a short excerpt of your own messages from this ChatGPT
export (not the assistant's replies) to your local `claude` CLI so it can
label each conversation's domain/topics/outcome. Nothing is sent to
Valuezen by this command itself; scoring happens in Valuezen's backend,
from raw counts, not from this label.
""".strip()


def _excerpt_for(conv, max_chars=6000):
    parts = [m["text"].strip() for m in _messages_of(conv) if m["role"] == "user" and m["text"].strip()]
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
        session_id = conv.get("conversation_id") or conv.get("id") or ""
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
