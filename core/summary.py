"""Print a human-readable tally of collected evidence — raw counts only,
no derived scores, ratios, or cost estimates. Scoring lives entirely in
Valuezen's scoring; this stays a plain accountant so
the plugin never computes anything that looks like a verdict.

Reads only common-schema event types, so this works the same regardless of
which adapter produced the evidence.
"""

import json

from .local_storage import STORE_DIR


def print_summary(source=None):
    """`source`, if given, limits the tally to that adapter's events — the
    store is shared by every adapter."""
    files = sorted(STORE_DIR.glob("*.jsonl")) if STORE_DIR.exists() else []
    if not files:
        print("No evidence collected yet. Run: collect.py setup")
        return

    sessions = set()
    event_counts = {}
    tokens_by_model = {}  # model -> {input, output, cache_read, cache_creation} — raw sums only
    tools = {}
    skills = {}
    agents = {}
    mcp_servers = {}
    files_changed_total = 0
    extensions_total = {}
    lines_added_total = 0
    lines_removed_total = 0
    languages_total = {}
    tests_total = 0
    tests_passed_total = 0
    total_duration = 0
    session_count_with_duration = 0
    multi_step_sessions = 0
    total_errors = 0
    reflections = []
    days = set()

    for fpath in files:
        with open(fpath) as f:
            for line in f:
                try:
                    ev = json.loads(line.strip())
                except Exception:
                    continue
                if source and ev.get("source") != source:
                    continue
                days.add(fpath.stem)
                etype = ev.get("event", "")
                event_counts[etype] = event_counts.get(etype, 0) + 1
                sessions.add(ev.get("session_id", ""))

                if etype == "ai_turn":
                    model = ev.get("model") or "(unknown)"
                    bucket = tokens_by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0})
                    bucket["input"] += ev.get("input_tokens", 0)
                    bucket["output"] += ev.get("output_tokens", 0)
                    bucket["cache_read"] += ev.get("cache_read_tokens", 0)
                    bucket["cache_creation"] += ev.get("cache_creation_tokens", 0)

                elif etype == "tool_call":
                    tool = ev.get("tool", "")
                    tools[tool] = tools.get(tool, 0) + 1

                elif etype == "skill_use":
                    s = ev.get("skill", "")
                    skills[s] = skills.get(s, 0) + 1

                elif etype == "agent_invoke":
                    a = ev.get("subagent_type", "")
                    agents[a] = agents.get(a, 0) + 1

                elif etype == "mcp_tool_call":
                    srv = ev.get("server", "")
                    mcp_servers[srv] = mcp_servers.get(srv, 0) + 1

                elif etype == "file_change":
                    files_changed_total += ev.get("files_count", 0)
                    lines_added_total += ev.get("lines_added", 0)
                    lines_removed_total += ev.get("lines_removed", 0)
                    lang = ev.get("language")
                    if lang:
                        languages_total[lang] = languages_total.get(lang, 0) + 1
                    for ext, cnt in (ev.get("extensions") or {}).items():
                        extensions_total[ext] = extensions_total.get(ext, 0) + cnt

                elif etype == "test_run":
                    tests_total += ev.get("tests", 0)
                    tests_passed_total += ev.get("passed", 0)

                elif etype == "session_end":
                    dur = ev.get("duration_seconds")
                    if dur:
                        total_duration += dur
                        session_count_with_duration += 1
                    if ev.get("multi_step"):
                        multi_step_sessions += 1
                    total_errors += ev.get("error_count", 0)

                elif etype == "session_reflect":
                    reflections.append(ev)

    if not days:
        print(f"No evidence collected yet for {source}.")
        return
    days = sorted(days)

    input_tokens = sum(b["input"] for b in tokens_by_model.values())
    output_tokens = sum(b["output"] for b in tokens_by_model.values())
    cache_tokens = sum(b["cache_read"] + b["cache_creation"] for b in tokens_by_model.values())

    print("AI Propensity Evidence — Raw Tally")
    print("=" * 40)
    print(f"Date range    : {days[0]}  →  {days[-1]}")
    print(f"Active days   : {len(days)}")
    print(f"Sessions      : {len(sessions)}")
    if multi_step_sessions:
        print(f"  Multi-step  : {multi_step_sessions}")
    print(f"Total events  : {sum(event_counts.values())}")
    if session_count_with_duration:
        avg_min = total_duration // session_count_with_duration // 60
        print(f"Avg duration  : ~{avg_min} min/session")

    print()
    print(f"AI turns      : {event_counts.get('ai_turn', 0)}")
    print(f"  Input tokens  : {input_tokens:,}")
    print(f"  Output tokens : {output_tokens:,}")
    print(f"  Cache tokens  : {cache_tokens:,}")
    if tokens_by_model:
        print("  By model (raw sums — cost/efficiency scoring happens in backend):")
        for model, b in sorted(tokens_by_model.items(), key=lambda x: -(x[1]["input"] + x[1]["output"])):
            print(f"    {model:<28} in={b['input']:>9,}  out={b['output']:>9,}  cache={b['cache_read'] + b['cache_creation']:>9,}")
    if total_errors:
        print(f"Tool errors   : {total_errors}")
    if tests_total:
        print(f"Test runs     : {tests_total} test-command invocations, {tests_passed_total} exited cleanly (structural signal — not a parsed test count)")

    print()
    if tools:
        print("Tools used:")
        for tool, cnt in sorted(tools.items(), key=lambda x: -x[1]):
            print(f"  {tool:<25} {cnt}")
    if skills:
        print("Skills invoked:")
        for skill, cnt in sorted(skills.items(), key=lambda x: -x[1]):
            print(f"  {skill:<25} {cnt}")
    if agents:
        print("Agents spawned:")
        for agent, cnt in sorted(agents.items(), key=lambda x: -x[1]):
            print(f"  {agent or '(default)':<25} {cnt}")
    if mcp_servers:
        print("MCP servers used:")
        for srv, cnt in sorted(mcp_servers.items(), key=lambda x: -x[1]):
            print(f"  {srv:<25} {cnt}")
    if files_changed_total:
        print(f"\nFiles modified: {files_changed_total}  (+{lines_added_total}/-{lines_removed_total} lines)")
        if languages_total:
            lang_str = ", ".join(f"{lang} x{cnt}" for lang, cnt in sorted(languages_total.items(), key=lambda x: -x[1]))
            print(f"  Languages: {lang_str}")
        if extensions_total:
            ext_str = ", ".join(f"{ext} x{cnt}" for ext, cnt in sorted(extensions_total.items(), key=lambda x: -x[1]))
            print(f"  Extensions: {ext_str}")

    if reflections:
        print()
        print(f"--- Advanced tier: {len(reflections)} session(s) LLM-tagged (opt-in, run via `classify`) ---")
        print("These carry a domain/topics/outcome label per session — categorical evidence,")
        print("not a score. Run `export` to include them; backend decides what to do with them.")
    else:
        print()
        print("Advanced tier: not run. `collect.py classify` opts in to LLM-based domain/topic/outcome tagging.")
