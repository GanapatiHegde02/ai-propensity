"""Package local evidence for explicit, user-triggered upload.

Nothing here runs automatically — export_evidence() only ever runs when the
user asks for it (a CLI command / skill invocation), and the caller decides
where the file lands.

Output shape is dictated by the consumer: Valuezen's upload page reads one
JSON file shaped {schema_version, events: [...]}. A zip of per-day JSONL
files can't be parsed there, so this must stay a single flat JSON file,
not an archive.

No event name/field translation happens here (there used to be one — see
git history — that renamed `files_count`->`files`,
`mcp_tool_call`->`tool_call`, `agent_invoke`->`agent_delegate`; removed
2026-09-26 because it silently zeroed `total_files_touched`, `mcp_calls`
and `agent_delegations` on every real export once
Valuezen's scoring was fixed to read the collector's own native
event/field names directly — caught by
running this exporter against 161k real events). The local store's event
names ARE what the scoring cards read; exporting is a pure flatten, no
reshaping.
"""

import datetime
import json
from pathlib import Path

from .event_schema import SCHEMA_VERSION, utcnow
from .local_storage import STORE_DIR

# The daily store files interleave every adapter's events together (one
# ~/.valuezen/propensity/events/<date>.jsonl per day, any source) — this is
# the full set of source values any adapter ever writes there.
VALID_SOURCES = {"claude_code", "codex", "claude_web", "chatgpt"}


def parse_export_args(argv):
    """Parse an adapter's `export [days] [--only src1,src2,...]` CLI args
    (pass sys.argv[2:]). Returns (days_or_None, only_sources_set_or_None).
    Unknown source names are kept as-is (export_evidence just won't match
    anything for them) rather than silently ignored, so a typo shows up as
    "0 events" instead of a quietly-wrong filter."""
    only_sources = None
    positional = []
    i = 0
    while i < len(argv):
        if argv[i] == "--only" and i + 1 < len(argv):
            only_sources = {s.strip() for s in argv[i + 1].split(",") if s.strip()}
            i += 2
            continue
        positional.append(argv[i])
        i += 1
    days = int(positional[0]) if positional else None
    return days, only_sources


def _file_date(fpath):
    try:
        return datetime.date.fromisoformat(fpath.stem)
    except ValueError:
        return None


def export_evidence(source, out_dir=None, days=None, only_sources=None):
    """Flatten this source's events from the daily evidence files into one
    {schema_version, events} JSON file under ~/.valuezen/<label>/.

    The store is shared by every adapter, so events from other sources are
    filtered out here — otherwise each adapter's upload would carry every
    tool's evidence under its own `source` label, and all adapters would
    overwrite the same file (found and fixed independently twice, 2026-09-26
    and 2026-09-27 — this is the merged version of both).

    By default (`only_sources=None`) this filters strictly to `source` —
    safe by default, no flag needed, so plain `export`/`sync` from any one
    adapter's CLI can never silently mix in another adapter's events.

    Pass `only_sources` (e.g. {"claude_web", "chatgpt"}) to combine
    specific sources into one export instead, regardless of which adapter's
    CLI this is called from — e.g. `--only claude_code,codex` to combine
    Claude Code + Codex, or `--only claude_web,chatgpt` to combine
    Claude.ai + ChatGPT. The output then lands under a combined-label directory
    (`~/.valuezen/claude_web+chatgpt/`) rather than `source`'s own, so a
    single-source export and a combined one never collide.

    `days`, if given, limits this to files dated within the last N days
    (today inclusive) — same cutoff convention as retention.prune(). None
    (the default) exports every file currently on disk.

    Returns (out_path, event_count), or (None, 0) if there is nothing to
    export (no files at all, or nothing matches the filter).
    """
    files = sorted(STORE_DIR.glob("*.jsonl")) if STORE_DIR.exists() else []
    if days is not None:
        cutoff = datetime.date.today() - datetime.timedelta(days=days)
        files = [fp for fp in files if _file_date(fp) is None or _file_date(fp) >= cutoff]
    if not files:
        return None, 0

    keep = only_sources if only_sources is not None else {source}
    events = []
    for fpath in files:
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("source") not in keep:
                    continue
                ev["schema_version"] = SCHEMA_VERSION
                events.append(ev)
    if not events:
        return None, 0

    label = "+".join(sorted(only_sources)) if only_sources else source
    out_dir = Path(out_dir) if out_dir else (Path.home() / ".valuezen" / label)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"propensity-evidence-{datetime.date.today().isoformat()}.json"

    tiers_included = ["basic"]
    if any(ev.get("event") == "session_reflect" for ev in events):
        tiers_included.append("advanced")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": label,
        "exported_at": utcnow(),
        "tiers_included": tiers_included,
        "events": events,
    }
    out_path.write_text(json.dumps(payload, indent=2))

    return out_path, len(events)
