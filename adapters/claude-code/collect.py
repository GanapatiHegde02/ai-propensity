#!/usr/bin/env python3
"""
AI Propensity Signal Collector — Claude Code Adapter

Extracts observable AI-usage signals from a local Claude Code installation
(~/.claude/projects transcripts, read by `setup`/`sync`) and normalizes them
into the common event schema defined in core/event_schema.py. This file is
adapter-specific; everything platform-agnostic (schema, storage, retention,
export, summary, LLM self-classification) lives in ../../core so other
adapters (claude-web, chatgpt, future codex) can reuse it without touching
this file.

Privacy-first: captures observable metadata only — tool names, token
counts, line-count deltas, file extensions/language — never prompt or
response content.
"""

import datetime
import json
import sys
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
ROOT_DIR = ADAPTER_DIR.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core import event_schema, local_storage, retention, export as core_export, summary, llm_classify, shell_signals  # noqa: E402

SOURCE = "claude_code"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
ERROR_LOG = Path.home() / ".valuezen" / "propensity" / "hook-errors.log"

# Bump whenever parse_conversation() changes what it extracts from the same
# transcript — `setup`/`sync` then replace every claude_code event written
# by the older parser instead of mixing old and new numbers.
#   2 (2026-10-05): one ai_turn per API response (message.id), not per
#     transcript line; user_turns counts typed prompts only; languages are
#     programming languages only; per-session `features`; live hooks no
#     longer write evidence.
PARSER_VERSION = 2

SUBAGENT_TOOLS = ("Agent", "Task")  # "Task" is the subagent tool's older name
WEB_TOOLS = ("WebSearch", "WebFetch")
PLAN_TOOLS = ("EnterPlanMode", "ExitPlanMode")

# `user`-type transcript lines that the person didn't type: tool results are
# handled separately (they're nested blocks, never text), these are the
# string-content ones — background-task notices, local command output,
# interrupt markers and injected reminders.
_NOT_TYPED_PREFIXES = (
    "<task-notification", "<local-command", "<bash-stdout", "<bash-stderr",
    "[Request interrupted", "<system-reminder",
)


def _typed_prompt_blocks(d):
    """For a `user` transcript line, the set of block kinds ("text",
    "image") if it's a prompt the person actually typed — or None if it's
    a tool result, a compaction summary, injected meta context or one of
    _NOT_TYPED_PREFIXES. Claude Code logs every tool result as its own
    `user` line, so counting every `user` line (the pre-2 behaviour)
    reported roughly 12x the real number of prompts."""
    if d.get("isMeta") or d.get("isCompactSummary") or d.get("toolUseResult") is not None:
        return None
    content = (d.get("message") or {}).get("content", "")
    if isinstance(content, str):
        text = content.strip()
        if not text or text.startswith(_NOT_TYPED_PREFIXES):
            return None
        return {"text"}
    if not isinstance(content, list):
        return None
    kinds = set()
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_result":
            return None
        if c.get("type") == "text":
            text = (c.get("text") or "").strip()
            if text and not text.startswith(_NOT_TYPED_PREFIXES):
                kinds.add("text")
        elif c.get("type") == "image":
            kinds.add("image")
    return kinds or None


def log_hook_error(context, exc):
    """Best-effort error log so `status` can surface hook failures instead of
    them vanishing silently (hooks must never raise back into Claude Code)."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a") as f:
            f.write(f"{event_schema.utcnow()} [{context}] {exc!r}\n")
    except Exception:
        pass


def _lines_for_tool_use(name, inp):
    """Line-count delta for a successful Edit/Write, derived from the tool
    call's own input and immediately discarded — only the counts and a
    language guess are kept, never the text. Edit's old/new-string
    line-count comparison is a coarse proxy, not a real diff."""
    file_path = inp.get("file_path", "")
    if not file_path:
        return 0, 0, None
    language = shell_signals.language_for(file_path)
    if name == "Write":
        content = inp.get("content", "") or ""
        return len(content.splitlines()), 0, language
    if name == "Edit":
        old = inp.get("old_string", "") or ""
        new = inp.get("new_string", "") or ""
        return len(new.splitlines()), len(old.splitlines()), language
    return 0, 0, None


# ---------------------------------------------------------------------------
# Live hooks — retired (parser version 2)
#
# The PostToolUse/Stop hooks used to write tool_call/file_change/session_end
# events as you worked. Two defects made that evidence wrong rather than
# merely early: (1) `setup`/`sync` then backfilled the same session from its
# transcript without removing the hook's events, so tool calls and files
# were counted twice; (2) Claude Code's Stop hook fires after every
# response, not once per session, and passes `transcript_path` rather than
# the `transcript` the handler read — so each response wrote another
# session_end with ai_turns 0. The transcript already holds everything the
# hooks saw, so `sync` is now the only writer. The commands stay as no-ops
# so an older install that still wires them doesn't error.
# ---------------------------------------------------------------------------

def _drain_stdin():
    try:
        sys.stdin.read()
    except Exception:
        pass


def cmd_hook():
    """No-op (see above) — kept for installs that still wire PostToolUse."""
    _drain_stdin()


def cmd_stop():
    """No-op (see above) — kept for installs that still wire Stop."""
    _drain_stdin()


# ---------------------------------------------------------------------------
# Backfill — parse ~/.claude/projects history
# ---------------------------------------------------------------------------

def parse_conversation(fpath, observed_at):
    events = []
    session_id = None
    session_start_ts = None
    session_end_ts = None
    first_ts = None
    last_ts = None
    # One entry per API response. Claude Code writes one transcript line per
    # content block (thinking, text, each tool_use), each repeating the same
    # message.id and usage — appending per line (the pre-2 behaviour)
    # counted every response and its tokens ~6x. Keyed by message.id; the
    # last line's usage wins, since it carries the final output count.
    ai_turns = {}
    features = set()
    tool_type_counts = {}
    error_count = 0
    bash_error_count = 0
    skills = []
    agents = []
    mcp_calls = []
    plain_tool_calls = []
    permission_mode = None
    model = None
    models_seen = set()
    user_turns = 0

    # tool_use -> tool_result correlation. tool_result blocks are nested
    # inside a *user*-type entry's message.content, never their own
    # top-level `type` — matching purely on `type == "tool_result"` (the
    # previous version of this function) silently never fires, so
    # error_count/bash_error_count were always 0 for backfilled sessions.
    pending_tool_uses = {}
    touched_files = set()
    lines_added = 0
    lines_removed = 0
    languages = {}
    tests_total = 0
    tests_passed = 0

    try:
        with open(fpath) as f:
            lines = f.readlines()
    except Exception:
        return []

    for line_no, line in enumerate(lines):
        try:
            d = json.loads(line.strip())
        except Exception:
            continue

        t = d.get("type", "")
        ts = d.get("timestamp", "")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        if t == "queue-operation":
            sid = d.get("sessionId", "")
            if sid:
                session_id = sid
            if d.get("operation") == "enqueue" and not session_start_ts:
                session_start_ts = ts
            if d.get("operation") == "dequeue":
                session_end_ts = ts

        elif t == "attachment":
            if ((d.get("attachment") or {}).get("type") or "").startswith("plan_mode"):
                features.add("plan_mode")

        elif t == "user":
            if not session_id:
                session_id = d.get("sessionId", "")
            if not permission_mode:
                permission_mode = d.get("permissionMode", "")
            if d.get("permissionMode") == "plan":
                features.add("plan_mode")
            typed = _typed_prompt_blocks(d)
            if typed:
                user_turns += 1
                if "image" in typed:
                    features.add("image_input")

            content = d.get("message", {}).get("content", "")
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict) or c.get("type") != "tool_result":
                        continue
                    is_err = bool(c.get("is_error"))
                    origin = pending_tool_uses.get(c.get("tool_use_id"), {})
                    oname = origin.get("name", "")
                    if is_err:
                        error_count += 1
                        if oname == "Bash":
                            bash_error_count += 1
                    if oname in ("Edit", "Write") and not is_err:
                        la, lr, lang = _lines_for_tool_use(oname, origin.get("input", {}))
                        fp = origin.get("input", {}).get("file_path", "")
                        if fp:
                            touched_files.add(fp)
                        lines_added += la
                        lines_removed += lr
                        if lang:
                            languages[lang] = languages.get(lang, 0) + 1
                    if oname == "Bash":
                        tr = shell_signals.test_run_for_command(origin.get("input", {}).get("command", ""), is_err)
                        if tr:
                            tests_total += tr[0]
                            tests_passed += tr[1]

        elif t == "assistant":
            msg = d.get("message", {})
            m = msg.get("model", "")
            if m:
                if not model:
                    model = m
                models_seen.add(m)
            usage = msg.get("usage", {}) or {}
            out_tokens = usage.get("output_tokens", 0)
            in_tokens = usage.get("input_tokens", 0)

            if (in_tokens or out_tokens) and m != "<synthetic>":
                key = msg.get("id") or f"line-{line_no}"
                prior = ai_turns.get(key)
                ai_turns[key] = {
                    "model": m or model,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "stop_reason": msg.get("stop_reason") or (prior or {}).get("stop_reason", ""),
                    "ts": (prior or {}).get("ts") or ts or session_start_ts,
                }

            for c in msg.get("content", []):
                if c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input", {})
                tool_type_counts[name] = tool_type_counts.get(name, 0) + 1
                pending_tool_uses[c.get("id")] = {"name": name, "input": inp}
                if name in WEB_TOOLS:
                    features.add("web_search")
                elif name in PLAN_TOOLS:
                    features.add("plan_mode")
                if name == "Skill":
                    skills.append(inp.get("skill", ""))
                elif name in SUBAGENT_TOOLS:
                    agents.append(inp.get("subagent_type", ""))
                elif name.startswith("mcp__"):
                    parts = name.split("__")
                    mcp_calls.append({
                        "server": parts[1] if len(parts) > 1 else "",
                        "tool": parts[2] if len(parts) > 2 else name,
                    })
                else:
                    # Plain tool calls (Bash, Read, Edit, Write, Grep, ...) —
                    # previously only counted into the aggregate
                    # tools_summary.tool_counts, never emitted as individual
                    # `tool_call` events. Nothing in
                    # Valuezen's scoring reads
                    # tools_summary; its Agentic Execution card counts
                    # individual `event == "tool_call"` entries, so every
                    # backfilled session's plain-tool-call count silently
                    # read as 0 until this was added.
                    plain_tool_calls.append(name)

    if not session_id:
        return []

    # Older transcripts have no queue-operation lines at all; fall back to
    # the first/last timestamped line rather than dating the whole session
    # to the moment of the backfill.
    session_start_ts = session_start_ts or first_ts
    session_end_ts = last_ts or session_end_ts
    base_ts = session_start_ts or observed_at

    if skills:
        features.add("skills")
    if agents:
        features.add("subagents")
    if mcp_calls:
        features.add("mcp")

    project_dir = fpath.parent
    has_project_instructions = any([
        (project_dir / "CLAUDE.md").exists(),
        (project_dir / ".claude" / "CLAUDE.md").exists(),
    ])

    events.append(event_schema.make_event("session_start", session_id, SOURCE, {
        "model": model,
        "permission_mode": permission_mode,
        "project": project_dir.name,
        "has_project_instructions": has_project_instructions,
    }, ts=base_ts, historical=True, observed_at=observed_at))

    for turn in ai_turns.values():
        events.append(event_schema.make_event("ai_turn", session_id, SOURCE, {
            "model": turn["model"],
            "input_tokens": turn["input_tokens"],
            "output_tokens": turn["output_tokens"],
            "cache_creation_tokens": turn["cache_creation_tokens"],
            "cache_read_tokens": turn["cache_read_tokens"],
            "stop_reason": turn["stop_reason"],
        }, ts=turn["ts"], historical=True, observed_at=observed_at))

    total_tool_calls = sum(tool_type_counts.values())

    for skill in skills:
        events.append(event_schema.make_event("skill_use", session_id, SOURCE, {
            "skill": skill,
        }, ts=base_ts, historical=True, observed_at=observed_at))

    for agent in agents:
        events.append(event_schema.make_event("agent_invoke", session_id, SOURCE, {
            "subagent_type": agent,
        }, ts=base_ts, historical=True, observed_at=observed_at))

    for mc in mcp_calls:
        events.append(event_schema.make_event("mcp_tool_call", session_id, SOURCE, {
            "server": mc["server"],
            "tool": mc["tool"],
        }, ts=base_ts, historical=True, observed_at=observed_at))

    for tool in plain_tool_calls:
        events.append(event_schema.make_event("tool_call", session_id, SOURCE, {
            "tool": tool,
        }, ts=base_ts, historical=True, observed_at=observed_at))

    if touched_files:
        events.append(event_schema.make_event("file_change", session_id, SOURCE, {
            "files_count": len(touched_files),
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "language": max(languages, key=languages.get) if languages else None,
            "languages": sorted(languages.keys()) if languages else [],
        }, ts=session_end_ts or base_ts, historical=True, observed_at=observed_at))

    if tests_total:
        events.append(event_schema.make_event("test_run", session_id, SOURCE, {
            "tests": tests_total,
            "passed": tests_passed,
        }, ts=session_end_ts or base_ts, historical=True, observed_at=observed_at))

    dur = event_schema.duration_seconds(session_start_ts, session_end_ts)

    events.append(event_schema.make_event("session_end", session_id, SOURCE, {
        "trigger": "backfill",
        "ai_turns": len(ai_turns),
        "user_turns": user_turns,
        "total_tool_calls": total_tool_calls,
        "tool_chain_length": total_tool_calls,
        "multi_step": total_tool_calls > 1,
        "duration_seconds": dur,
        "error_count": error_count,
        "bash_error_count": bash_error_count,
        "models_used": sorted(m for m in models_seen if m != "<synthetic>"),
        # Which optional capabilities this session used — presence only,
        # never how often. Keys: subagents, mcp, skills, plan_mode,
        # web_search, image_input. Feeds the Feature Exploration vector.
        "features": sorted(features),
    }, ts=session_end_ts or base_ts, historical=True, observed_at=observed_at))

    return events


def cmd_backfill():
    """Incremental by file mtime, not just presence — a session captured
    once but still growing (e.g. one you're still in) gets re-parsed and
    its stale partial capture replaced, instead of being skipped forever.
    A session whose transcript hasn't changed since last time is skipped
    cheaply, before the (expensive) full parse."""
    if not CLAUDE_PROJECTS.exists():
        print("No ~/.claude/projects directory found.")
        return

    observed_at = event_schema.utcnow()
    new_sessions = 0
    refreshed_sessions = 0
    skipped = 0
    total_events = 0

    # Also clears any events the retired live hooks wrote (never
    # `historical`), which would otherwise double-count against the backfill.
    reparse_all = local_storage.reset_source_if_parser_changed(SOURCE, PARSER_VERSION)
    if reparse_all:
        print(f"Collector updated (parser v{PARSER_VERSION}) — re-reading all Claude Code history once.")

    parsed = []  # (session_id, mtime, events)
    to_refresh = set()
    for project_dir in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project_dir.is_dir():
            continue
        for conv_file in sorted(project_dir.glob("*.jsonl")):
            session_id = conv_file.stem
            current_mtime = conv_file.stat().st_mtime
            recorded_mtime = None if reparse_all else local_storage.backfill_mtime(session_id)

            if recorded_mtime is not None and current_mtime <= recorded_mtime:
                skipped += 1
                continue

            evs = parse_conversation(conv_file, observed_at)
            if not evs:
                continue

            if recorded_mtime is not None:
                to_refresh.add(session_id)
                to_refresh.update(ev["session_id"] for ev in evs)
                refreshed_sessions += 1
            else:
                new_sessions += 1
            parsed.append((session_id, current_mtime, evs))

    # One pass over the store for every grown session, not one per session.
    local_storage.remove_sessions_events(to_refresh)

    for session_id, current_mtime, evs in parsed:
        for ev in evs:
            date_str = ev.get("ts", "")[:10] or datetime.date.today().isoformat()
            local_storage.append_event(ev, date_str)
            total_events += 1
        local_storage.set_backfill_mtime(session_id, current_mtime)

    local_storage.set_parser_version(SOURCE, PARSER_VERSION)

    print("Backfill complete.")
    print(f"  New sessions        : {new_sessions}")
    print(f"  Refreshed sessions  : {refreshed_sessions} (grown since last capture)")
    print(f"  Unchanged, skipped  : {skipped}")
    print(f"  Events written      : {total_events}")
    print(f"  Store location      : {local_storage.STORE_DIR}")


# ---------------------------------------------------------------------------
# Snapshot — capture current in-progress session mid-flight
# ---------------------------------------------------------------------------

def find_active_session_file():
    """
    Find the JSONL file for the current active session.
    Strategy:
      1. Look for session_ids in STATE_DIR (only ever written by the retired
         live hooks, so normally empty now).
      2. Cross-reference with ~/.claude/projects/**/<session_id>.jsonl.
      3. Fall back to the most recently modified JSONL in projects if no state match.
    Returns (session_id, Path) or (None, None).
    """
    if local_storage.STATE_DIR.exists():
        state_files = sorted(local_storage.STATE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for sf in state_files:
            sid = sf.stem
            matches = list(CLAUDE_PROJECTS.glob(f"*/{sid}.jsonl"))
            if matches:
                return sid, matches[0]

    all_jsonl = list(CLAUDE_PROJECTS.glob("*/*.jsonl")) if CLAUDE_PROJECTS.exists() else []
    if all_jsonl:
        newest = max(all_jsonl, key=lambda p: p.stat().st_mtime)
        return newest.stem, newest

    return None, None


def cmd_snapshot():
    """Write a point-in-time session_snapshot event for the current active session."""
    session_id, fpath = find_active_session_file()

    if not fpath:
        print("No active session file found in ~/.claude/projects/")
        return

    now = event_schema.utcnow()
    usage_by_response = {}  # message.id -> usage, same de-duplication as parse_conversation()
    tool_counts = {}
    user_turns = 0
    models_seen = set()
    files_changed = set()
    session_start_ts = None
    model = None
    permission_mode = None

    try:
        with open(fpath) as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Could not read session file: {e}")
        return

    for line_no, line in enumerate(lines):
        try:
            d = json.loads(line.strip())
        except Exception:
            continue

        t = d.get("type", "")
        ts = d.get("timestamp", "")
        if ts and not session_start_ts:
            session_start_ts = ts

        if t == "queue-operation":
            sid = d.get("sessionId", "")
            if sid:
                session_id = sid

        elif t == "user":
            if _typed_prompt_blocks(d):
                user_turns += 1
            if not permission_mode:
                permission_mode = d.get("permissionMode", "")

        elif t == "assistant":
            msg = d.get("message", {})
            m = msg.get("model", "")
            if m and m != "<synthetic>":
                if not model:
                    model = m
                models_seen.add(m)
            usage = msg.get("usage", {}) or {}
            if (usage.get("input_tokens") or usage.get("output_tokens")) and m != "<synthetic>":
                usage_by_response[msg.get("id") or f"line-{line_no}"] = usage
            for c in msg.get("content", []):
                if c.get("type") == "tool_use":
                    name = c.get("name", "")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    if name in ("Edit", "Write"):
                        fp = c.get("input", {}).get("file_path", "")
                        if fp:
                            files_changed.add(fp)

    ai_turns_count = len(usage_by_response)
    input_tokens = sum(u.get("input_tokens", 0) for u in usage_by_response.values())
    output_tokens = sum(u.get("output_tokens", 0) for u in usage_by_response.values())
    cache_tokens = sum(
        u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        for u in usage_by_response.values()
    )
    total_tool_calls = sum(tool_counts.values())
    elapsed = event_schema.duration_seconds(session_start_ts, now)

    ev = {
        "schema_version": event_schema.SCHEMA_VERSION,
        "ts": now,
        "event": "session_snapshot",
        "source": SOURCE,
        "session_id": session_id,
        "provenance": "snapshot",
        "model": model,
        "permission_mode": permission_mode,
        "ai_turns": ai_turns_count,
        "user_turns": user_turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tool_calls": total_tool_calls,
        "tool_counts": tool_counts,
        "multi_step": total_tool_calls > 1,
        "files_changed": len(files_changed),
        "models_used": sorted(models_seen),
        "elapsed_seconds": elapsed,
        "session_file": str(fpath),
    }

    local_storage.append_event(ev)

    print("Session snapshot captured.")
    print(f"  Session      : {session_id[:8]}…")
    print(f"  AI turns     : {ai_turns_count}  (in: {input_tokens:,}  out: {output_tokens:,}  cache: {cache_tokens:,})")
    print(f"  User turns   : {user_turns}")
    print(f"  Tool calls   : {total_tool_calls}  multi-step: {total_tool_calls > 1}")
    print(f"  Files touched: {len(files_changed)}")
    if elapsed:
        print(f"  Elapsed      : {elapsed // 60}m {elapsed % 60}s")
    if models_seen:
        print(f"  Models       : {', '.join(sorted(models_seen))}")


# ---------------------------------------------------------------------------
# Classify — deferred. Implementation kept for a future release, but not
# wired into COMMANDS below, so it is not reachable from the CLI.
# ---------------------------------------------------------------------------

CONSENT_NOTICE = """
This will send a short excerpt of your own prompts (not Claude's replies,
not full transcripts, no file contents) to your local `claude` CLI so it
can label each session's domain/topics/outcome. This is different from
every other command in this tool, which reads only metadata (tool names,
token counts, line-count deltas).

The result is stored locally as a "session_reflect" event, tagged
tier="advanced" — categorical labels, not a score. It's only included in an
export if you choose to export it. Nothing is sent to Valuezen by this
command itself; scoring of any kind happens in Valuezen's backend, from
raw counts, not from this label.
""".strip()


def _session_excerpt(fpath, max_chars=6000):
    """A bounded, user-turn-only text excerpt for classification — never
    stored, never written to the evidence store, discarded after the call
    to core.llm_classify.classify_excerpt() returns."""
    parts = []
    try:
        with open(fpath) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                except Exception:
                    continue
                if d.get("type") == "user":
                    content = d.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                        )
                    if isinstance(content, str) and content.strip():
                        parts.append(content.strip())
    except Exception:
        return ""
    return "\n".join(parts)[:max_chars]


def cmd_classify():
    if not llm_classify.claude_cli_available():
        print("`claude` CLI not found on PATH — classify needs it to run headless self-classification.")
        return

    if not llm_classify.get_consent():
        print(CONSENT_NOTICE)
        print()
        answer = input("Proceed and remember this choice? [y/N] ").strip().lower()
        if answer != "y":
            print("Cancelled. No content was sent.")
            return
        llm_classify.record_consent()

    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    classified = skipped = scanned = 0

    if not CLAUDE_PROJECTS.exists():
        print("No ~/.claude/projects directory found.")
        return

    candidates = []
    for project_dir in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project_dir.is_dir():
            continue
        candidates.extend(project_dir.glob("*.jsonl"))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for conv_file in candidates:
        if classified >= limit:
            break
        session_id = conv_file.stem
        scanned += 1
        if local_storage.already_classified(session_id):
            skipped += 1
            continue
        excerpt = _session_excerpt(conv_file)
        ev = llm_classify.classify_excerpt(excerpt, session_id, SOURCE, on_error=log_hook_error)
        if ev is None:
            continue
        date_str = ev.get("ts", "")[:10] or datetime.date.today().isoformat()
        local_storage.append_event(ev, date_str)
        classified += 1
        print(f"  {session_id[:8]}…  domain={ev['domain']:<20} outcome={ev['outcome']:<10} topics={', '.join(ev['topics'])}")

    print()
    print(f"Classified {classified} session(s), skipped {skipped} already-classified, scanned {scanned}.")
    print("Run 'summary' to see the advanced-tier rollup, or 'export' to include it in an upload.")


# ---------------------------------------------------------------------------
# Retention / prune
# ---------------------------------------------------------------------------

def cmd_prune():
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None
    removed, days_used = retention.prune(days)
    if removed:
        print(f"Pruned {len(removed)} file(s) older than {days_used} days:")
        for name in removed:
            print(f"  {name}")
    else:
        print(f"Nothing to prune — no files older than {days_used} days.")


def cmd_retention():
    """View or set the persisted retention period."""
    if len(sys.argv) > 2:
        try:
            days = int(sys.argv[2])
        except ValueError:
            print("Usage: collect.py retention [days]")
            return
        retention.set_retention_days(days)
        print(f"Retention set to {days} days. Run 'prune' to apply immediately.")
    else:
        print(f"Retention is currently {retention.get_retention_days()} days.")
        print("Change with: collect.py retention <days>")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def cmd_status():
    settings_path = Path.home() / ".claude" / "settings.json"
    print("AI Propensity Collector — Status")
    print("=" * 40)

    if settings_path.exists():
        try:
            with open(settings_path) as f:
                cfg = json.load(f)
            hooks = cfg.get("hooks", {})
            post = any(
                "collect.py" in h.get("command", "")
                for entry in hooks.get("PostToolUse", [])
                for h in entry.get("hooks", [])
            )
            stop = any(
                "collect.py" in h.get("command", "")
                for entry in hooks.get("Stop", [])
                for h in entry.get("hooks", [])
            )
            # Hooks are retired (see "Live hooks — retired" above): `sync`
            # reads the transcripts directly, so nothing is missing without them.
            print("Live hooks       : not needed — run 'sync' to collect")
            if post or stop:
                print("                   (older install still wires them; they're harmless no-ops)")
        except Exception:
            print("settings.json    : could not parse")
    else:
        print("settings.json    : not found")

    print()
    files = sorted(local_storage.STORE_DIR.glob("*.jsonl")) if local_storage.STORE_DIR.exists() else []
    total = sum(1 for fp in files for _ in open(fp))
    print(f"Evidence store   : {local_storage.STORE_DIR}")
    print(f"Daily files      : {len(files)}")
    print(f"Total events     : {total}")
    print(f"Retention        : {retention.get_retention_days()} days")
    print(f"Schema version   : {event_schema.SCHEMA_VERSION}")

    if ERROR_LOG.exists():
        try:
            error_lines = ERROR_LOG.read_text().splitlines()
        except Exception:
            error_lines = []
        if error_lines:
            print()
            print(f"Hook errors      : {len(error_lines)} logged — see {ERROR_LOG}")
            print(f"  Last: {error_lines[-1]}")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def cmd_export():
    days, only_sources = core_export.parse_export_args(sys.argv[2:])
    out_path, count = core_export.export_evidence(SOURCE, days=days, only_sources=only_sources)
    if not out_path:
        print("No evidence to export.")
        return
    print(f"Exported {count} event(s)" + (f" from the last {days} day(s)." if days else "."))
    if only_sources:
        print(f"Filtered to source(s): {', '.join(sorted(only_sources))}")
    print(f"File: {out_path}")


# ---------------------------------------------------------------------------
# Sync — the one command a user actually needs to run
# ---------------------------------------------------------------------------

def cmd_sync():
    """setup + export in one call: pick up whatever's new since last time,
    then write the ready-to-upload JSON. This is what `/ai-collect:run
    sync` should be — everything else is a building block for it."""
    print("Collecting new sessions...")
    cmd_backfill()
    print()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None
    out_path, count = core_export.export_evidence(SOURCE, days=days)
    if not out_path:
        print("Nothing to export yet.")
        return
    print()
    print(f"Ready to upload: {out_path}  ({count} events)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "hook":      cmd_hook,
    "stop":      cmd_stop,
    "setup":     cmd_backfill,
    "sync":      cmd_sync,
    "snapshot":  cmd_snapshot,
    "summary":   lambda: summary.print_summary(SOURCE),
    "status":    cmd_status,
    "export":    cmd_export,
    "prune":     cmd_prune,
    "retention": cmd_retention,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd not in COMMANDS:
        print(f"Usage: collect.py [{' | '.join(COMMANDS)}]", file=sys.stderr)
        sys.exit(1)
    COMMANDS[cmd]()
