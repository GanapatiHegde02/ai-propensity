"""Regression tests for the adapters' parsers, built from small synthetic
samples in each tool's real on-disk format (shapes copied from real
transcripts/rollouts/exports, contents invented). Every case here is a
defect that once shipped silently — a format change should fail a test,
not quietly skew someone's report.

Run from the repo root:  python3 -m pytest tests
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import local_storage, shell_signals  # noqa: E402


def _load(adapter):
    spec = importlib.util.spec_from_file_location(
        f"adapter_{adapter.replace('-', '_')}", ROOT / "adapters" / adapter / "collect.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


claude_code = _load("claude-code")
codex = _load("codex")
chatgpt = _load("chatgpt")
claude_web = _load("claude-web")


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _by_event(events, name):
    return [e for e in events if e["event"] == name]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage, "STORE_DIR", tmp_path / "events")
    monkeypatch.setattr(local_storage, "STATE_DIR", tmp_path / ".state")
    monkeypatch.setattr(local_storage, "BACKFILL_MANIFEST", tmp_path / ".state" / "_backfill_manifest.json")
    return tmp_path


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def _cc_assistant(msg_id, ts, blocks, usage):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1",
            "message": {"id": msg_id, "model": "claude-opus-5-5", "content": blocks, "usage": usage}}


def _cc_user(ts, content, **extra):
    row = {"type": "user", "timestamp": ts, "sessionId": "s1", "message": {"role": "user", "content": content}}
    row.update(extra)
    return row


def test_claude_code_counts_each_response_once_and_only_typed_prompts(tmp_path):
    usage_partial = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50}
    usage_final = {"input_tokens": 10, "output_tokens": 300, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50}
    rows = [
        _cc_user("2026-10-01T10:00:00Z", "Refactor the parser", permissionMode="plan"),
        _cc_user("2026-10-01T10:00:01Z", "<system-reminder>injected</system-reminder>", isMeta=True),
        # One response logged as three lines (thinking, text, tool_use) — one ai_turn.
        _cc_assistant("msg_1", "2026-10-01T10:00:05Z", [{"type": "thinking", "thinking": ""}], usage_partial),
        _cc_assistant("msg_1", "2026-10-01T10:00:06Z", [{"type": "text", "text": "ok"}], usage_partial),
        _cc_assistant("msg_1", "2026-10-01T10:00:07Z", [
            {"type": "tool_use", "id": "tu_1", "name": "Write",
             "input": {"file_path": "/repo/app.py", "content": "a\nb\nc"}}], usage_final),
        # Tool result arrives as a `user` line — not a prompt.
        _cc_user("2026-10-01T10:00:08Z", [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}],
                 toolUseResult={"type": "create"}),
        _cc_assistant("msg_2", "2026-10-01T10:00:09Z", [
            {"type": "tool_use", "id": "tu_2", "name": "Write",
             "input": {"file_path": "/repo/README.md", "content": "x"}},
        ], usage_final),
        _cc_user("2026-10-01T10:00:10Z", [{"type": "tool_result", "tool_use_id": "tu_2", "content": "ok"}],
                 toolUseResult={"type": "create"}),
        _cc_assistant("msg_3", "2026-10-01T10:00:11Z", [
            {"type": "tool_use", "id": "tu_3", "name": "WebSearch", "input": {"query": "q"}},
            {"type": "tool_use", "id": "tu_4", "name": "Agent", "input": {"subagent_type": "Explore"}},
        ], usage_final),
        _cc_user("2026-10-01T10:01:00Z", [{"type": "text", "text": "what about this?"},
                                         {"type": "image", "source": {"type": "base64", "data": ""}}]),
        _cc_user("2026-10-01T10:01:01Z", "[Request interrupted by user]"),
        _cc_user("2026-10-01T10:01:02Z", "summary", isCompactSummary=True),
    ]
    fpath = _write_jsonl(tmp_path / "s1.jsonl", rows)
    events = claude_code.parse_conversation(fpath, "2026-10-05T00:00:00Z")

    turns = _by_event(events, "ai_turn")
    assert len(turns) == 3
    assert turns[0]["output_tokens"] == 300  # the response's final usage, counted once

    end = _by_event(events, "session_end")[0]
    assert end["user_turns"] == 2
    assert end["ai_turns"] == 3
    assert set(end["features"]) == {"plan_mode", "web_search", "subagents", "image_input"}

    change = _by_event(events, "file_change")[0]
    assert change["files_count"] == 2           # README still counts as a file touched...
    assert change["languages"] == ["python"]    # ...but not as a language

    assert events[0]["ts"] == "2026-10-01T10:00:00Z"  # no queue-operation lines: first timestamp, not backfill time


def test_claude_code_live_hooks_write_nothing(store, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "tool_name": "Bash"})))
    claude_code.cmd_hook()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "transcript_path": "/x"})))
    claude_code.cmd_stop()
    assert not list((store / "events").glob("*.jsonl")) if (store / "events").exists() else True


def test_claude_code_backfill_replaces_events_from_older_parser(store, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    _write_jsonl(projects / "p" / "s1.jsonl", [
        _cc_user("2026-10-01T10:00:00Z", "hello"),
        _cc_assistant("m1", "2026-10-01T10:00:01Z", [{"type": "text", "text": "hi"}],
                      {"input_tokens": 1, "output_tokens": 1}),
    ])
    monkeypatch.setattr(claude_code, "CLAUDE_PROJECTS", projects)
    # Evidence left by an older version: a live-hook event and a per-line ai_turn.
    local_storage.append_event({"ts": "2026-10-01T10:00:00Z", "event": "tool_call", "source": "claude_code", "session_id": "s1"}, "2026-10-01")
    local_storage.append_event({"ts": "2026-10-01T10:00:00Z", "event": "ai_turn", "source": "claude_code", "session_id": "s1"}, "2026-10-01")
    local_storage.append_event({"ts": "2026-10-01T10:00:00Z", "event": "ai_turn", "source": "codex", "session_id": "other"}, "2026-10-01")

    claude_code.cmd_backfill()
    stored = [json.loads(l) for f in (store / "events").glob("*.jsonl") for l in f.read_text().splitlines()]
    cc = [e for e in stored if e["source"] == "claude_code"]
    assert not [e for e in cc if e["event"] == "tool_call"]
    assert len([e for e in cc if e["event"] == "ai_turn"]) == 1
    assert [e for e in stored if e["source"] == "codex"], "other sources' evidence must be untouched"

    claude_code.cmd_backfill()  # unchanged transcript: nothing duplicated
    stored2 = [json.loads(l) for f in (store / "events").glob("*.jsonl") for l in f.read_text().splitlines()]
    assert len(stored2) == len(stored)


def test_language_allow_list():
    assert shell_signals.language_for("a/b.py") == "python"
    assert shell_signals.language_for("a/b.tsx") == "typescript"
    for not_code in ("README.md", "notes.txt", "config.json", "values.yaml", ".env.prod", "x.example", "Makefile"):
        assert shell_signals.language_for(not_code) is None


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def _cx(t, ts, payload):
    return {"type": t, "timestamp": ts, "payload": payload}


def _totals(inp, cached, out):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
            "reasoning_output_tokens": 0, "total_tokens": inp + out}


def _token_count(ts, totals):
    return _cx("event_msg", ts, {"type": "token_count", "info": {"total_token_usage": totals, "last_token_usage": totals}})


PATCH = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-old\n+new\n+more\n*** Add File: docs/notes.md\n+hi\n*** End Patch"


def test_codex_current_format(tmp_path):
    rows = [
        _cx("session_meta", "2026-10-01T10:00:00Z", {"id": "x", "cwd": "/repo", "originator": "codex_vscode"}),
        _cx("turn_context", "2026-10-01T10:00:00Z", {"model": "gpt-5.5", "collaboration_mode": {"mode": "plan"}}),
        _cx("response_item", "2026-10-01T10:00:00Z", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}),
        _cx("response_item", "2026-10-01T10:00:00Z", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "# AGENTS.md instructions"}]}),
        _cx("response_item", "2026-10-01T10:00:01Z", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "# Context from my IDE setup:\n## My request for Codex:\nfix it"},
            {"type": "input_image", "image_url": "data:"}]}),
        _token_count("2026-10-01T10:00:02Z", _totals(1000, 800, 50)),
        _token_count("2026-10-01T10:00:02Z", _totals(1000, 800, 50)),  # repeated — same response
        _cx("event_msg", "2026-10-01T10:00:02Z", {"type": "token_count", "info": None}),
        _cx("response_item", "2026-10-01T10:00:03Z", {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch", "input": PATCH}),
        _cx("response_item", "2026-10-01T10:00:03Z", {"type": "custom_tool_call_output", "call_id": "c1",
            "output": json.dumps({"output": "Success. Updated the following files:\nM src/app.py\n", "metadata": {}})}),
        _cx("response_item", "2026-10-01T10:00:04Z", {"type": "function_call", "call_id": "c2", "name": "shell_command",
            "arguments": json.dumps({"command": "python -m pytest -q", "workdir": "/repo"})}),
        _cx("response_item", "2026-10-01T10:00:05Z", {"type": "function_call_output", "call_id": "c2",
            "output": "Exit code: 1\nWall time: 1 seconds\nOutput:\n1 failed"}),
        _cx("response_item", "2026-10-01T10:00:06Z", {"type": "web_search_call", "status": "completed"}),
        _token_count("2026-10-01T10:00:07Z", _totals(2500, 2000, 120)),
        _cx("turn_context", "2026-10-01T10:00:08Z", {"model": "gpt-5.3-codex"}),
        _cx("response_item", "2026-10-01T10:00:09Z", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "now add tests"}]}),
    ]
    events = codex.parse_conversation(_write_jsonl(tmp_path / "rollout-1.jsonl", rows), "2026-10-05T00:00:00Z")

    turns = _by_event(events, "ai_turn")
    assert len(turns) == 2
    # Sum over responses equals the session's own final total; cached input
    # is split out of input_tokens.
    assert sum(t["input_tokens"] for t in turns) == 2500 - 2000
    assert sum(t["cache_read_tokens"] for t in turns) == 2000
    assert sum(t["output_tokens"] for t in turns) == 120

    end = _by_event(events, "session_end")[0]
    assert end["user_turns"] == 2
    assert set(end["features"]) == {"plan_mode", "image_input", "web_search"}
    assert end["models_used"] == ["gpt-5.3-codex", "gpt-5.5"]

    change = _by_event(events, "file_change")[0]
    assert change["files_count"] == 2
    assert (change["lines_added"], change["lines_removed"]) == (3, 1)
    assert change["languages"] == ["python"]

    tools = {e["tool"]: e["success"] for e in _by_event(events, "tool_call")}
    assert tools == {"apply_patch": True, "shell_command": False, "web_search": True}
    test_run = _by_event(events, "test_run")[0]
    assert (test_run["tests"], test_run["passed"]) == (1, 0)


def test_codex_older_format_still_parses(tmp_path):
    rows = [
        _cx("session_meta", "2026-03-01T10:00:00Z", {"id": "x", "cwd": "/repo", "originator": "codex_cli_rs"}),
        _cx("turn_context", "2026-03-01T10:00:00Z", {"model": "gpt-5.1-codex"}),
        _cx("event_msg", "2026-03-01T10:00:01Z", {"type": "user_message", "message": "do it"}),
        _cx("event_msg", "2026-03-01T10:00:02Z", {"type": "patch_apply_end", "call_id": "p1", "success": True,
            "changes": {"lib/x.go": {"unified_diff": "--- a\n+++ b\n-a\n+b\n"}}}),
        _token_count("2026-03-01T10:00:03Z", _totals(100, 0, 10)),
    ]
    events = codex.parse_conversation(_write_jsonl(tmp_path / "rollout-old.jsonl", rows), "2026-10-05T00:00:00Z")
    end = _by_event(events, "session_end")[0]
    assert end["user_turns"] == 1
    change = _by_event(events, "file_change")[0]
    assert change["files_count"] == 1 and change["languages"] == ["go"]


def test_codex_subagent_sessions_skipped(tmp_path):
    rows = [_cx("session_meta", "2026-10-01T10:00:00Z", {"id": "x", "cwd": "/r", "thread_source": "subagent"})]
    assert codex.parse_conversation(_write_jsonl(tmp_path / "r.jsonl", rows), "2026-10-05T00:00:00Z") == []


# ---------------------------------------------------------------------------
# ChatGPT / Claude.ai exports
# ---------------------------------------------------------------------------

def _gpt_node(role, text=None, content=None, metadata=None, author_name=None, recipient=None, t=1.0):
    author = {"role": role}
    if author_name:
        author["name"] = author_name
    return {"message": {
        "author": author, "create_time": t, "recipient": recipient,
        "content": content or {"content_type": "text", "parts": [text or ""]},
        "metadata": metadata or {},
    }}


def test_chatgpt_features():
    conv = {"conversation_id": "c1", "conversation_template_id": "g-p-123", "plugin_ids": [], "mapping": {
        "a": _gpt_node("user", "hi", metadata={"attachments": [{"mime_type": "application/pdf"}, {"mime_type": "image/png"}]}),
        "b": _gpt_node("assistant", content={"content_type": "thoughts", "thoughts": []}),
        "c": _gpt_node("assistant", "answer", metadata={"search_result_groups": [{}], "model_slug": "gpt-5"}),
        "d": _gpt_node("assistant", "report", metadata={"async_task_title": "Research"}),
        "e": _gpt_node("tool", "", author_name="canmore.create_textdoc"),
    }}
    assert chatgpt._features_of(conv) == {
        "projects", "file_upload", "image_input", "reasoning", "web_search", "deep_research", "created_files",
    }
    plain = {"conversation_id": "c2", "conversation_template_id": "g-68e5f1dea7", "mapping": {
        "a": _gpt_node("user", "hi"), "b": _gpt_node("assistant", "hello", metadata={"model_slug": "gpt-5"}),
    }}
    assert chatgpt._features_of(plain) == set()


def test_claude_web_features():
    conv = {"uuid": "u1", "chat_messages": [
        {"sender": "human", "text": "hi", "attachments": [{"file_type": "txt"}], "files": [{"file_name": "shot.PNG"}]},
        {"sender": "assistant", "text": "x", "content": [
            {"type": "thinking", "thinking": ""},
            {"type": "tool_use", "name": "web_search"},
            {"type": "tool_use", "name": "create_file"},
            {"type": "tool_use", "name": "project_knowledge_search"},
            {"type": "tool_use", "name": "google_drive_search"},
        ]},
    ]}
    assert claude_web._features_of(conv) == {
        "file_upload", "image_input", "reasoning", "web_search", "created_files", "projects", "connectors",
    }


def test_chat_import_replaces_changed_conversations(store, tmp_path, monkeypatch):
    def export(update_time, n_user):
        mapping = {}
        for i in range(n_user):
            mapping[f"u{i}"] = _gpt_node("user", "q" * 10, t=float(i * 2 + 1))
            mapping[f"a{i}"] = _gpt_node("assistant", "a" * 20, t=float(i * 2 + 2))
        path = tmp_path / f"conversations-{update_time}.json"
        path.write_text(json.dumps([{"conversation_id": "c1", "create_time": 1.0, "update_time": update_time, "mapping": mapping}]))
        return str(path)

    monkeypatch.setattr(sys, "argv", ["collect.py", "import", export(100.0, 1)])
    chatgpt.cmd_import()
    monkeypatch.setattr(sys, "argv", ["collect.py", "import", export(200.0, 3)])  # same chat, two more messages
    chatgpt.cmd_import()
    monkeypatch.setattr(sys, "argv", ["collect.py", "import", export(200.0, 3)])  # unchanged — skipped
    chatgpt.cmd_import()

    stored = [json.loads(l) for f in (store / "events").glob("*.jsonl") for l in f.read_text().splitlines()]
    ends = [e for e in stored if e["event"] == "session_end"]
    assert len(ends) == 1 and ends[0]["user_turns"] == 3
    assert len([e for e in stored if e["event"] == "ai_turn"]) == 3
    assert "edit_count" not in ends[0]
