#!/usr/bin/env python3
"""
AI Propensity Signal Collector — Claude Code Adapter

Extracts observable AI-usage signals from a local Claude Code installation
(~/.claude/projects transcripts, PostToolUse/Stop hooks) and normalizes them
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
# Live hook handlers
# ---------------------------------------------------------------------------

def cmd_hook():
    """PostToolUse hook — fires after every tool call. Accumulates
    per-session running counts (errors, line deltas, test results) into
    local state; cmd_stop flushes them into file_change/test_run events."""
    try:
        data = json.loads(sys.stdin.read())
    except Exception as exc:
        log_hook_error("hook:parse_stdin", exc)
        return

    try:
        session_id = data.get("session_id", "unknown")
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", {}) or {}
        is_error = tool_response.get("is_error", False)

        state = local_storage.read_state(session_id)
        if "start_ts" not in state:
            state["start_ts"] = event_schema.utcnow()
        if is_error:
            state["error_count"] = state.get("error_count", 0) + 1
            if tool_name == "Bash":
                state["bash_error_count"] = state.get("bash_error_count", 0) + 1

        if tool_name in ("Edit", "Write") and not is_error:
            la, lr, lang = _lines_for_tool_use(tool_name, tool_input)
            touched = set(state.get("touched_files", []))
            fp = tool_input.get("file_path", "")
            if fp:
                touched.add(fp)
            state["touched_files"] = sorted(touched)
            state["lines_added"] = state.get("lines_added", 0) + la
            state["lines_removed"] = state.get("lines_removed", 0) + lr
            if lang:
                langs = state.get("languages", {})
                langs[lang] = langs.get(lang, 0) + 1
                state["languages"] = langs

        if tool_name == "Bash":
            tr = shell_signals.test_run_for_command(tool_input.get("command", ""), is_error)
            if tr:
                invocations, clean_exits = tr
                state["tests"] = state.get("tests", 0) + invocations
                state["tests_passed"] = state.get("tests_passed", 0) + clean_exits

        local_storage.write_state(session_id, state)

        if tool_name == "Skill":
            ev = event_schema.make_event("skill_use", session_id, SOURCE, {
                "skill": tool_input.get("skill", ""),
                "success": not is_error,
            })
        elif tool_name == "Agent":
            ev = event_schema.make_event("agent_invoke", session_id, SOURCE, {
                "subagent_type": tool_input.get("subagent_type", ""),
                "run_in_background": bool(tool_input.get("run_in_background", False)),
                "success": not is_error,
            })
        elif tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            ev = event_schema.make_event("mcp_tool_call", session_id, SOURCE, {
                "server": parts[1] if len(parts) > 1 else "",
                "tool": parts[2] if len(parts) > 2 else tool_name,
                "success": not is_error,
            })
        else:
            ev = event_schema.make_event("tool_call", session_id, SOURCE, {
                "tool": tool_name,
                "success": not is_error,
            })

        local_storage.append_event(ev)
    except Exception as exc:
        log_hook_error("hook:process", exc)


def cmd_stop():
    """Stop hook — fires when a Claude Code session ends. Parses transcript
    for tokens, flushes cmd_hook's accumulated file/test state."""
    try:
        data = json.loads(sys.stdin.read())
    except Exception as exc:
        log_hook_error("stop:parse_stdin", exc)
        return

    try:
        session_id = data.get("session_id", "unknown")
        transcript = data.get("transcript", [])
        state = local_storage.read_state(session_id)
        now = event_schema.utcnow()

        ai_turns = []
        user_turns = 0
        tool_counts = {}
        models_seen = set()

        for entry in transcript:
            t = entry.get("type", "")
            if t == "user":
                user_turns += 1
            elif t == "assistant":
                msg = entry.get("message", {})
                model = msg.get("model", "")
                if model:
                    models_seen.add(model)
                usage = msg.get("usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                if in_tok or out_tok:
                    ai_turns.append({
                        "model": model,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                        "stop_reason": msg.get("stop_reason", ""),
                        "ts": entry.get("timestamp", now),
                    })
                for c in msg.get("content", []):
                    if c.get("type") == "tool_use":
                        name = c.get("name", "")
                        tool_counts[name] = tool_counts.get(name, 0) + 1

        for turn in ai_turns:
            local_storage.append_event(event_schema.make_event("ai_turn", session_id, SOURCE, {
                "model": turn["model"],
                "input_tokens": turn["input_tokens"],
                "output_tokens": turn["output_tokens"],
                "cache_creation_tokens": turn["cache_creation_tokens"],
                "cache_read_tokens": turn["cache_read_tokens"],
                "stop_reason": turn["stop_reason"],
            }, ts=turn["ts"]))

        total_tool_calls = sum(tool_counts.values())
        dur = event_schema.duration_seconds(state.get("start_ts"), now)

        touched_files = state.get("touched_files", [])
        if touched_files:
            languages = state.get("languages", {})
            local_storage.append_event(event_schema.make_event("file_change", session_id, SOURCE, {
                "files_count": len(touched_files),
                "lines_added": state.get("lines_added", 0),
                "lines_removed": state.get("lines_removed", 0),
                "language": max(languages, key=languages.get) if languages else None,
            }))

        if state.get("tests"):
            local_storage.append_event(event_schema.make_event("test_run", session_id, SOURCE, {
                "tests": state.get("tests", 0),
                "passed": state.get("tests_passed", 0),
            }))

        local_storage.append_event(event_schema.make_event("session_end", session_id, SOURCE, {
            "trigger": "stop_hook",
            "ai_turns": len(ai_turns),
            "user_turns": user_turns,
            "total_tool_calls": total_tool_calls,
            "tool_chain_length": total_tool_calls,
            "multi_step": total_tool_calls > 1,
            "duration_seconds": dur,
            "error_count": state.get("error_count", 0),
            "bash_error_count": state.get("bash_error_count", 0),
            "models_used": sorted(models_seen),
        }))

        local_storage.delete_state(session_id)
    except Exception as exc:
        log_hook_error("stop:process", exc)


# ---------------------------------------------------------------------------
# Backfill — parse ~/.claude/projects history
# ---------------------------------------------------------------------------

def parse_conversation(fpath, observed_at):
    events = []
    session_id = None
    session_start_ts = None
    session_end_ts = None
    ai_turns = []
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

    for line in lines:
        try:
            d = json.loads(line.strip())
        except Exception:
            continue

        t = d.get("type", "")
        ts = d.get("timestamp", "")

        if t == "queue-operation":
            sid = d.get("sessionId", "")
            if sid:
                session_id = sid
            if d.get("operation") == "enqueue" and not session_start_ts:
                session_start_ts = ts
            if d.get("operation") == "dequeue":
                session_end_ts = ts

        elif t == "user":
            if not session_id:
                session_id = d.get("sessionId", "")
            if not permission_mode:
                permission_mode = d.get("permissionMode", "")
            user_turns += 1

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
            usage = msg.get("usage", {})
            out_tokens = usage.get("output_tokens", 0)
            in_tokens = usage.get("input_tokens", 0)

            if in_tokens or out_tokens:
                ai_turns.append({
                    "model": m or model,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "stop_reason": msg.get("stop_reason", ""),
                    "ts": ts or session_start_ts,
                })

            for c in msg.get("content", []):
                if c.get("type") != "tool_use":
                    continue
                name = c.get("name", "")
                inp = c.get("input", {})
                tool_type_counts[name] = tool_type_counts.get(name, 0) + 1
                pending_tool_uses[c.get("id")] = {"name": name, "input": inp}
                if name == "Skill":
                    skills.append(inp.get("skill", ""))
                elif name == "Agent":
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

    base_ts = session_start_ts or observed_at

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

    for turn in ai_turns:
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
        "models_used": sorted(models_seen),
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

    for project_dir in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project_dir.is_dir():
            continue
        for conv_file in sorted(project_dir.glob("*.jsonl")):
            session_id = conv_file.stem
            current_mtime = conv_file.stat().st_mtime
            recorded_mtime = local_storage.backfill_mtime(session_id)

            if recorded_mtime is not None and current_mtime <= recorded_mtime:
                skipped += 1
                continue

            evs = parse_conversation(conv_file, observed_at)
            if not evs:
                continue

            if recorded_mtime is not None:
                local_storage.remove_session_events(session_id)
                refreshed_sessions += 1
            else:
                new_sessions += 1

            for ev in evs:
                date_str = ev.get("ts", "")[:10] or datetime.date.today().isoformat()
                local_storage.append_event(ev, date_str)
                total_events += 1

            local_storage.set_backfill_mtime(session_id, current_mtime)

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
      1. Look for session_ids in STATE_DIR (written by PostToolUse hook, deleted at Stop).
         These are sessions actively in progress right now.
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
    ai_turns_count = 0
    input_tokens = output_tokens = cache_tokens = 0
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

    for line in lines:
        try:
            d = json.loads(line.strip())
        except Exception:
            continue

        t = d.get("type", "")
        ts = d.get("timestamp", "")

        if t == "queue-operation":
            if d.get("operation") == "enqueue" and not session_start_ts:
                session_start_ts = ts
            sid = d.get("sessionId", "")
            if sid:
                session_id = sid

        elif t == "user":
            user_turns += 1
            if not permission_mode:
                permission_mode = d.get("permissionMode", "")

        elif t == "assistant":
            msg = d.get("message", {})
            m = msg.get("model", "")
            if m:
                if not model:
                    model = m
                models_seen.add(m)
            usage = msg.get("usage", {})
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            if in_tok or out_tok:
                ai_turns_count += 1
                input_tokens += in_tok
                output_tokens += out_tok
                cache_tokens += usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            for c in msg.get("content", []):
                if c.get("type") == "tool_use":
                    name = c.get("name", "")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    if name in ("Edit", "Write"):
                        fp = c.get("input", {}).get("file_path", "")
                        if fp:
                            files_changed.add(fp)

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
            print(f"PostToolUse hook : {'✓ wired' if post else '✗ missing'}")
            print(f"Stop hook        : {'✓ wired' if stop else '✗ missing'}")
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
