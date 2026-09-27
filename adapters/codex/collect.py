#!/usr/bin/env python3
"""
AI Propensity Signal Collector — Codex Adapter

Built and verified against real local history: ~/.codex/sessions/**/*.jsonl,
written by both Codex CLI and the Codex VS Code extension (same rollout
format — confirmed against actual files from both `codex_vscode` and
`codex_cli_rs` originators on this machine, 2026-09-24).

Unlike Claude Code, Codex has no documented plugin/hook API to attach a
live PostToolUse/Stop hook to — so this adapter is backfill-only: it parses
the rollout JSONL Codex already writes to disk on every session, the same
way claude-code's `setup`/`sync` parses ~/.claude/projects transcripts.
Practically this means the two "look the same" from a user's point of view
(same `sync` command, same output file) despite having no shared parsing
code — the transcript shapes are unrelated.

Rollout event shapes actually observed (not the publicly-documented spec —
this machine's real files):
  session_meta   payload: {id, session_id, cwd, originator, cli_version,
                            thread_source ("user"/"subagent"/absent)}
  turn_context   payload: {turn_id, model, cwd, ...}      — one per turn
  event_msg      payload.type: user_message, agent_message, task_started,
                                task_complete, token_count, patch_apply_end,
                                context_compacted, turn_aborted, ...
  response_item  payload.type: message (role user/assistant/developer),
                                function_call (name: exec_command,
                                view_image, write_stdin, update_plan),
                                function_call_output, custom_tool_call
                                (name: apply_patch), reasoning, ...

`thread_source: "subagent"` sessions (e.g. an internal risk-judging pass)
are skipped entirely — they're not the user's own usage.

Privacy-first, same as every adapter: tool names, token counts (Codex's
own token_count events give real, not-estimated input/output/cached
numbers), file-change line deltas (from apply_patch's unified diff) and
language, and a structural test-run signal (did an exec_command matching a
known test runner run, did it exit 0 — via "Process exited with code N" in
its own output, never a parsed pass/fail count — see
for why). Never prompt or
response content, except the opt-in `classify` command.
"""

import datetime
import json
import re
import sys
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
ROOT_DIR = ADAPTER_DIR.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core import event_schema, local_storage, retention, export as core_export, summary, llm_classify, shell_signals  # noqa: E402

SOURCE = "codex"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

EXIT_CODE_RE = re.compile(r"Process exited with code (\d+)")


# ---------------------------------------------------------------------------
# Backfill — parse ~/.codex/sessions rollout files
# ---------------------------------------------------------------------------

def parse_conversation(fpath, observed_at):
    events = []
    session_id = fpath.stem  # unique per file, unlike payload.session_id (shared across a resumed thread's files)
    cwd = None
    originator = None
    cli_version = None
    session_start_ts = None
    session_end_ts = None
    user_turns = 0
    ai_turns = []
    turn_models = {}
    turn_token_usage = {}  # turn_id -> running {input, output, cached, reasoning}
    current_turn_id = None
    pending_calls = {}  # call_id -> {"name": str, "cmd": str|None}
    tool_events = []  # (name, success)
    touched_files = set()
    lines_added = 0
    lines_removed = 0
    languages = {}
    tests_total = 0
    tests_passed = 0
    error_count = 0

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
        if ts:
            if not session_start_ts:
                session_start_ts = ts
            session_end_ts = ts
        p = d.get("payload", {}) or {}

        if t == "session_meta":
            if p.get("thread_source") == "subagent":
                return []  # internal judge/guardian pass, not the user's own usage
            cwd = p.get("cwd") or cwd
            originator = p.get("originator") or originator
            cli_version = p.get("cli_version") or cli_version

        elif t == "turn_context":
            turn_id = p.get("turn_id")
            if turn_id:
                turn_models[turn_id] = p.get("model")

        elif t == "event_msg":
            pt = p.get("type")

            if pt == "user_message":
                user_turns += 1

            elif pt == "task_started":
                current_turn_id = p.get("turn_id")
                turn_token_usage.setdefault(current_turn_id, {"input": 0, "output": 0, "cached": 0, "reasoning": 0})

            elif pt == "token_count":
                last = ((p.get("info") or {}).get("last_token_usage")) or {}
                bucket = turn_token_usage.setdefault(current_turn_id, {"input": 0, "output": 0, "cached": 0, "reasoning": 0})
                bucket["input"] += last.get("input_tokens", 0)
                bucket["output"] += last.get("output_tokens", 0)
                bucket["cached"] += last.get("cached_input_tokens", 0)
                bucket["reasoning"] += last.get("reasoning_output_tokens", 0)

            elif pt == "task_complete":
                turn_id = p.get("turn_id")
                usage = turn_token_usage.get(turn_id, {})
                if usage.get("input") or usage.get("output"):
                    ai_turns.append({
                        "model": turn_models.get(turn_id, ""),
                        "input_tokens": usage.get("input", 0),
                        "output_tokens": usage.get("output", 0),
                        "cache_read_tokens": usage.get("cached", 0),
                        "reasoning_output_tokens": usage.get("reasoning", 0),
                        "ts": ts or session_start_ts,
                    })

            elif pt == "patch_apply_end":
                success = bool(p.get("success", True))
                tool_events.append(("apply_patch", success))
                if not success:
                    error_count += 1
                for file_path, change in (p.get("changes") or {}).items():
                    touched_files.add(file_path)
                    lang = shell_signals.language_for(file_path)
                    if lang:
                        languages[lang] = languages.get(lang, 0) + 1
                    diff = change.get("unified_diff", "") or ""
                    for dl in diff.splitlines():
                        if dl.startswith("+++") or dl.startswith("---"):
                            continue
                        if dl.startswith("+"):
                            lines_added += 1
                        elif dl.startswith("-"):
                            lines_removed += 1

        elif t == "response_item":
            pt = p.get("type")

            if pt == "function_call":
                call_id = p.get("call_id")
                name = p.get("name", "")
                cmd = None
                if name == "exec_command":
                    try:
                        cmd = json.loads(p.get("arguments", "{}")).get("cmd")
                    except Exception:
                        cmd = None
                pending_calls[call_id] = {"name": name, "cmd": cmd}

            elif pt == "function_call_output":
                call_id = p.get("call_id")
                origin = pending_calls.pop(call_id, {})
                name = origin.get("name", "tool")
                output = p.get("output", "") or ""
                if not isinstance(output, str):
                    output = json.dumps(output)  # some tools (e.g. view_image) return structured output, not text
                is_error = False
                m = EXIT_CODE_RE.search(output)
                if m:
                    is_error = m.group(1) != "0"
                if is_error:
                    error_count += 1
                tool_events.append((name, not is_error))
                if name == "exec_command" and origin.get("cmd"):
                    tr = shell_signals.test_run_for_command(origin["cmd"], is_error)
                    if tr:
                        tests_total += tr[0]
                        tests_passed += tr[1]

    if not any(l.strip() for l in lines):
        return []
    # A file with no session_meta at all isn't a real session (e.g. a
    # truncated/corrupt write) — cwd staying None the whole time is the tell,
    # since every real session_meta observed on this machine carries cwd.
    if cwd is None and originator is None:
        return []

    base_ts = session_start_ts or observed_at
    project = Path(cwd).name if cwd else ""

    events.append(event_schema.make_event("session_start", session_id, SOURCE, {
        "model": next((m for m in turn_models.values() if m), None),
        "project": project,
        "originator": originator,
        "cli_version": cli_version,
    }, ts=base_ts, historical=True, observed_at=observed_at))

    for turn in ai_turns:
        events.append(event_schema.make_event("ai_turn", session_id, SOURCE, {
            "model": turn["model"],
            "input_tokens": turn["input_tokens"],
            "output_tokens": turn["output_tokens"],
            "cache_read_tokens": turn["cache_read_tokens"],
            "reasoning_output_tokens": turn["reasoning_output_tokens"],
        }, ts=turn["ts"], historical=True, observed_at=observed_at))

    for name, success in tool_events:
        events.append(event_schema.make_event("tool_call", session_id, SOURCE, {
            "tool": name,
            "success": success,
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
    total_tool_calls = len(tool_events)

    events.append(event_schema.make_event("session_end", session_id, SOURCE, {
        "trigger": "backfill",
        "ai_turns": len(ai_turns),
        "user_turns": user_turns,
        "total_tool_calls": total_tool_calls,
        "tool_chain_length": total_tool_calls,
        "multi_step": total_tool_calls > 1,
        "duration_seconds": dur,
        "error_count": error_count,
        "bash_error_count": error_count,  # exec_command is Codex's only shell-tool signal, same role as Claude Code's Bash
        "models_used": sorted({m for m in turn_models.values() if m}),
    }, ts=session_end_ts or base_ts, historical=True, observed_at=observed_at))

    return events


def cmd_backfill():
    """Incremental by file mtime — same skip-if-unchanged / re-parse-if-grown
    behavior as claude-code's `setup`, keyed by rollout filename (unique per
    file; a resumed thread can share the same payload.session_id across
    multiple files, so the filename — not that field — is what this adapter
    tracks and uses as the event session_id)."""
    if not CODEX_SESSIONS.exists():
        print("No ~/.codex/sessions directory found — is Codex installed and has it been run at least once?")
        return

    observed_at = event_schema.utcnow()
    new_sessions = refreshed_sessions = skipped = total_events = skipped_subagent = 0

    for conv_file in sorted(CODEX_SESSIONS.glob("**/*.jsonl")):
        session_id = conv_file.stem
        current_mtime = conv_file.stat().st_mtime
        recorded_mtime = local_storage.backfill_mtime(session_id)

        if recorded_mtime is not None and current_mtime <= recorded_mtime:
            skipped += 1
            continue

        evs = parse_conversation(conv_file, observed_at)
        if not evs:
            skipped_subagent += 1
            local_storage.set_backfill_mtime(session_id, current_mtime)
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
    print(f"  Skipped (subagent/empty) : {skipped_subagent}")
    print(f"  Events written      : {total_events}")
    print(f"  Store location      : {local_storage.STORE_DIR}")


# ---------------------------------------------------------------------------
# Classify — opt-in, advanced-tier LLM self-classification (see
# core/llm_classify.py for the shared mechanics and consent flow)
# ---------------------------------------------------------------------------

CONSENT_NOTICE = """
This will send a short excerpt of your own messages from this Codex history
(not Codex's replies, no file contents) to your local `claude` CLI so it
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
    parts = []
    try:
        with open(fpath) as f:
            for line in f:
                try:
                    d = json.loads(line.strip())
                except Exception:
                    continue
                if d.get("type") == "event_msg" and (d.get("payload") or {}).get("type") == "user_message":
                    msg = d["payload"].get("message", "")
                    if isinstance(msg, str) and msg.strip():
                        parts.append(msg.strip())
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
        if input("Proceed and remember this choice? [y/N] ").strip().lower() != "y":
            print("Cancelled. No content was sent.")
            return
        llm_classify.record_consent()

    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    classified = skipped = 0

    if not CODEX_SESSIONS.exists():
        print("No ~/.codex/sessions directory found.")
        return

    candidates = sorted(CODEX_SESSIONS.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    for conv_file in candidates:
        if classified >= limit:
            break
        session_id = conv_file.stem
        if local_storage.already_classified(session_id):
            skipped += 1
            continue
        excerpt = _session_excerpt(conv_file)
        if not excerpt:
            continue
        ev = llm_classify.classify_excerpt(excerpt, session_id, SOURCE)
        if ev is None:
            continue
        date_str = ev.get("ts", "")[:10] or datetime.date.today().isoformat()
        local_storage.append_event(ev, date_str)
        classified += 1
        print(f"  {session_id[:8]}…  domain={ev['domain']:<20} outcome={ev['outcome']:<10} topics={', '.join(ev['topics'])}")

    print()
    print(f"Classified {classified} session(s), skipped {skipped}.")


# ---------------------------------------------------------------------------
# Export / sync / prune / retention / status
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


def cmd_sync():
    """setup + export in one call — the one command to point a user at.
    Mirrors adapters/claude-code's `sync`."""
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


def cmd_prune():
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None
    removed, days_used = retention.prune(days)
    print(f"Pruned {len(removed)} file(s) older than {days_used} days." if removed
          else f"Nothing to prune — no files older than {days_used} days.")


def cmd_retention():
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


def cmd_status():
    print("AI Propensity Collector (Codex) — Status")
    print("=" * 40)
    print(f"~/.codex/sessions   : {'found' if CODEX_SESSIONS.exists() else 'NOT FOUND'}")
    if CODEX_SESSIONS.exists():
        n = sum(1 for _ in CODEX_SESSIONS.glob("**/*.jsonl"))
        print(f"Rollout files        : {n}")
    print()
    files = sorted(local_storage.STORE_DIR.glob("*.jsonl")) if local_storage.STORE_DIR.exists() else []
    total = sum(1 for fp in files for _ in open(fp))
    print(f"Evidence store       : {local_storage.STORE_DIR}")
    print(f"Daily files          : {len(files)}")
    print(f"Total events         : {total}")
    print(f"Retention            : {retention.get_retention_days()} days")
    print(f"Schema version       : {event_schema.SCHEMA_VERSION}")
    print()
    print("No live hook — Codex has no documented hook/plugin API to attach to.")
    print("Run 'sync' any time to pick up what's changed since last time.")


COMMANDS = {
    "setup":     cmd_backfill,
    "sync":      cmd_sync,
    "classify":  cmd_classify,
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
