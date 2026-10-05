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
  event_msg      payload.type: token_count, task_started, task_complete,
                                turn_aborted, ...; older versions also wrote
                                user_message and patch_apply_end, which
                                current Codex no longer does (2026-10)
  response_item  payload.type: message (role user/assistant/developer —
                                user prompts live here now, alongside
                                injected environment/AGENTS.md context),
                                function_call (exec_command, shell_command,
                                view_image, write_stdin, update_plan),
                                custom_tool_call (apply_patch — every file
                                edit), *_output, web_search_call, reasoning

`thread_source: "subagent"` sessions (e.g. an internal risk-judging pass)
are skipped entirely — they're not the user's own usage.

Privacy-first, same as every adapter: tool names, token counts (Codex's
own token_count events give real, not-estimated input/output/cached
numbers), file-change line deltas (from apply_patch's unified diff) and
language, and a structural test-run signal (did an exec_command matching a
known test runner run, did it exit 0 — via the exit code Codex prints in
its own output, never a parsed pass/fail count). Never prompt or response
content.
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

# Bump whenever parse_conversation() changes what it extracts from the same
# rollout file — `setup`/`sync` then replace every codex event written by the
# older parser instead of mixing old and new numbers.
#   2 (2026-10-05): user prompts read from `response_item` user messages
#     (newer Codex no longer writes event_msg user_message); file changes
#     and tool calls read from `custom_tool_call` apply_patch (newer Codex
#     no longer writes patch_apply_end); one ai_turn per model response with
#     tokens taken from the running total_token_usage (summing
#     last_token_usage over-counted, since token_count is often repeated);
#     input_tokens now excludes cached input, matching Claude Code's
#     meaning; per-session `features`; programming languages only.
PARSER_VERSION = 2

# Older Codex prints "Process exited with code N"; newer prints "Exit code: N".
EXIT_CODE_RE = re.compile(r"(?:Process exited with code|Exit code:)\s*(\d+)")

# `response_item` user messages that Codex injects rather than the person
# typing them. A message counts as a prompt if any of its parts is real
# input (typed text or an attached image). VS Code's "# Context from my IDE"
# wrapper *is* a typed prompt — the request follows the IDE context.
_INJECTED_PREFIXES = (
    "<environment_context", "# AGENTS.md", "<user_instructions", "<turn_aborted",
    "<permissions", "<user_shell_command",
)
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")


def _typed_user_message(payload):
    """For a `response_item` user message: (is_prompt, has_image)."""
    is_prompt = has_image = False
    for c in payload.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "input_image":
            is_prompt = has_image = True
        elif c.get("type") == "input_text":
            text = (c.get("text") or "").lstrip()
            if text and not text.startswith(_INJECTED_PREFIXES):
                is_prompt = True
    return is_prompt, has_image


def _patch_stats(patch_text):
    """(files, lines_added, lines_removed) from an apply_patch body. Counts
    only the +/- lines inside hunks — never stores the text."""
    files, added, removed = set(), 0, 0
    for ln in (patch_text or "").splitlines():
        m = _PATCH_FILE_RE.match(ln)
        if m:
            files.add(m.group(1).strip())
        elif ln.startswith("***"):
            continue
        elif ln.startswith("+"):
            added += 1
        elif ln.startswith("-"):
            removed += 1
    return files, added, removed


def _output_text(output):
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                return parsed["output"]
        except Exception:
            pass
        return output
    return json.dumps(output)  # some tools (e.g. view_image) return structured output, not text


def _call_failed(output_text):
    m = EXIT_CODE_RE.search(output_text)
    if m:
        return m.group(1) != "0"
    return False


def _patch_failed(output_text):
    if "Success." in output_text:
        return False
    return _call_failed(output_text) or "failed" in output_text.lower()


def _shell_cmd(arguments):
    try:
        args = json.loads(arguments or "{}")
    except Exception:
        return None
    cmd = args.get("cmd") or args.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else None


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
    prompts_from_items = 0     # response_item user messages (current format)
    prompts_from_events = 0    # event_msg user_message (older format)
    ai_turns = []
    models = set()
    current_model = None
    features = set()
    last_totals = None         # running total_token_usage as of the previous ai_turn
    pending_calls = {}         # call_id -> {"name": str, "cmd": str|None, "patch": str|None}
    patched_call_ids = set()   # apply_patch calls already counted (both formats may log one patch)
    tool_events = []           # (name, success)
    touched_files = set()
    lines_added = 0
    lines_removed = 0
    languages = {}
    tests_total = 0
    tests_passed = 0
    error_count = 0

    def _record_patch(files, added, removed):
        nonlocal lines_added, lines_removed
        for file_path in files:
            touched_files.add(file_path)
            lang = shell_signals.language_for(file_path)
            if lang:
                languages[lang] = languages.get(lang, 0) + 1
        lines_added += added
        lines_removed += removed

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
            current_model = p.get("model") or current_model
            if current_model:
                models.add(current_model)
            if ((p.get("collaboration_mode") or {}).get("mode")) == "plan":
                features.add("plan_mode")

        elif t == "event_msg":
            pt = p.get("type")

            if pt == "user_message":
                prompts_from_events += 1
                if p.get("images"):
                    features.add("image_input")

            elif pt == "token_count":
                # One model response = one increase in the running total.
                # token_count is often written twice per response (and with
                # info null at turn boundaries), so it's the change in
                # total_token_usage — not each event's last_token_usage —
                # that marks a response and carries its tokens. Summed over
                # the session this equals Codex's own final total exactly.
                totals = ((p.get("info") or {}).get("total_token_usage")) or {}
                if not totals.get("total_tokens"):
                    continue
                prev = last_totals or {}
                if totals.get("total_tokens", 0) <= prev.get("total_tokens", 0):
                    continue
                d_in = totals.get("input_tokens", 0) - prev.get("input_tokens", 0)
                d_cached = totals.get("cached_input_tokens", 0) - prev.get("cached_input_tokens", 0)
                d_out = totals.get("output_tokens", 0) - prev.get("output_tokens", 0)
                d_reason = totals.get("reasoning_output_tokens", 0) - prev.get("reasoning_output_tokens", 0)
                last_totals = totals
                ai_turns.append({
                    "model": current_model or "",
                    # Codex's input_tokens includes the cached part; split
                    # it so input_tokens means fresh input, as it does for
                    # Claude Code, and cache_read_tokens the reused part.
                    "input_tokens": max(0, d_in - d_cached),
                    "output_tokens": max(0, d_out),
                    "cache_read_tokens": max(0, d_cached),
                    "reasoning_output_tokens": max(0, d_reason),
                    "ts": ts or session_start_ts,
                })

            elif pt == "patch_apply_end":
                # Older format: a separate event per applied patch.
                call_id = p.get("call_id")
                if call_id and call_id in patched_call_ids:
                    continue
                if call_id:
                    patched_call_ids.add(call_id)
                success = bool(p.get("success", True))
                tool_events.append(("apply_patch", success))
                if not success:
                    error_count += 1
                    continue
                files, added, removed = set(), 0, 0
                for file_path, change in (p.get("changes") or {}).items():
                    files.add(file_path)
                    for dl in (change.get("unified_diff", "") or "").splitlines():
                        if dl.startswith("+++") or dl.startswith("---"):
                            continue
                        if dl.startswith("+"):
                            added += 1
                        elif dl.startswith("-"):
                            removed += 1
                _record_patch(files, added, removed)

        elif t == "response_item":
            pt = p.get("type")

            if pt == "message" and p.get("role") == "user":
                is_prompt, has_image = _typed_user_message(p)
                if is_prompt:
                    prompts_from_items += 1
                if has_image:
                    features.add("image_input")

            elif pt == "web_search_call":
                features.add("web_search")
                tool_events.append(("web_search", True))

            elif pt in ("function_call", "custom_tool_call"):
                name = p.get("name", "")
                pending_calls[p.get("call_id")] = {
                    "name": name,
                    "cmd": _shell_cmd(p.get("arguments")) if name in ("exec_command", "shell_command", "shell") else None,
                    "patch": p.get("input") if name == "apply_patch" else None,
                }
                if name == "apply_patch" and p.get("arguments") and not p.get("input"):
                    try:  # function_call form carries the patch inside arguments
                        pending_calls[p.get("call_id")]["patch"] = json.loads(p["arguments"]).get("input")
                    except Exception:
                        pass

            elif pt in ("function_call_output", "custom_tool_call_output"):
                call_id = p.get("call_id")
                origin = pending_calls.pop(call_id, None)
                if origin is None:
                    continue
                name = origin.get("name") or "tool"
                out = _output_text(p.get("output", "") or "")

                if name == "apply_patch":
                    if call_id in patched_call_ids:
                        continue
                    patched_call_ids.add(call_id)
                    failed = _patch_failed(out)
                    tool_events.append(("apply_patch", not failed))
                    if failed:
                        error_count += 1
                    else:
                        _record_patch(*_patch_stats(origin.get("patch")))
                    continue

                failed = _call_failed(out)
                if failed:
                    error_count += 1
                tool_events.append((name, not failed))
                if origin.get("cmd"):
                    tr = shell_signals.test_run_for_command(origin["cmd"], failed)
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

    # Both formats can coexist in one file; whichever saw more prompts is
    # the one this Codex version actually wrote.
    user_turns = max(prompts_from_items, prompts_from_events)

    base_ts = session_start_ts or observed_at
    project = Path(cwd).name if cwd else ""

    events.append(event_schema.make_event("session_start", session_id, SOURCE, {
        "model": next(iter(sorted(models)), None),
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
            "languages": sorted(languages.keys()) if languages else [],
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
        "bash_error_count": error_count,  # shell commands are Codex's main tool, same role as Claude Code's Bash
        "models_used": sorted(models),
        # Which optional capabilities this session used — presence only.
        # Keys: plan_mode, web_search, image_input. Feeds Feature Exploration.
        "features": sorted(features),
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

    reparse_all = local_storage.reset_source_if_parser_changed(SOURCE, PARSER_VERSION)
    if reparse_all:
        print(f"Collector updated (parser v{PARSER_VERSION}) — re-reading all Codex history once.")

    parsed = []  # (session_id, mtime, events)
    to_refresh = set()
    for conv_file in sorted(CODEX_SESSIONS.glob("**/*.jsonl")):
        session_id = conv_file.stem
        current_mtime = conv_file.stat().st_mtime
        recorded_mtime = None if reparse_all else local_storage.backfill_mtime(session_id)

        if recorded_mtime is not None and current_mtime <= recorded_mtime:
            skipped += 1
            continue

        evs = parse_conversation(conv_file, observed_at)
        if not evs:
            skipped_subagent += 1
            local_storage.set_backfill_mtime(session_id, current_mtime)
            continue

        if recorded_mtime is not None:
            to_refresh.add(session_id)
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
    print(f"  Skipped (subagent/empty) : {skipped_subagent}")
    print(f"  Events written      : {total_events}")
    print(f"  Store location      : {local_storage.STORE_DIR}")


# ---------------------------------------------------------------------------
# Classify — deferred. Implementation kept for a future release, but not
# wired into COMMANDS below, so it is not reachable from the CLI.
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
                p = d.get("payload") or {}
                if d.get("type") == "event_msg" and p.get("type") == "user_message":
                    msg = p.get("message", "")
                    if isinstance(msg, str) and msg.strip():
                        parts.append(msg.strip())
                elif d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                    for c in p.get("content") or []:
                        text = (c.get("text") or "").strip() if isinstance(c, dict) and c.get("type") == "input_text" else ""
                        if text and not text.startswith(_INJECTED_PREFIXES):
                            parts.append(text)
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
