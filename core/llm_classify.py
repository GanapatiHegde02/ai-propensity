"""Shared 'advanced tier' machinery: shell a bounded text excerpt to the
user's own local `claude` CLI and get back a small categorical label —
domain / topics / task_type / outcome. Deliberately NOT a score: no
complexity number, no effectiveness percentage. Those are computed in
Valuezen, from raw counts, where the formulas can change without a
plugin release. This module only asks the one question raw counts can't
answer: what was this about, and did it land.

Every adapter (claude-code, claude-web, chatgpt, future codex) shares this
one function — the classifying model doesn't need to be the same product
that produced the conversation, it's just being asked to read text and
label it. Each adapter only needs its own `build_excerpt()` to turn its
source format into a bounded string.

Never wired into a hook. Only runs when a user explicitly invokes `classify`,
and only after consent (see get/record_consent below) — see any adapter's
CONSENT_NOTICE for exactly what that means.
"""

import json
import shutil
import subprocess

from . import event_schema, retention

VALID_OUTCOMES = {"completed", "partial", "abandoned", "unclear"}
VALID_TASK_TYPES = {"single-step", "multi-step"}


def get_consent():
    return bool(retention.get_config().get("llm_classify_consent"))


def record_consent():
    retention.update_config(llm_classify_consent=True)


def claude_cli_available():
    return shutil.which("claude") is not None


def _prompt_for(excerpt):
    return (
        "Classify this AI-assistant conversation excerpt. Respond with ONLY "
        "a single JSON object, no prose, matching exactly this shape: "
        '{"domain": "<one short lowercase phrase for what this was about, '
        'your own words, e.g. \'coding\' or \'trip planning\'>", '
        '"topics": [up to 5 short lowercase tags], '
        '"task_type": "single-step" or "multi-step", '
        '"outcome": "completed" or "partial" or "abandoned" or "unclear", '
        '"outcome_rationale": <=15 words}.\n\n'
        f"Excerpt:\n{excerpt}"
    )


def classify_excerpt(excerpt, session_id, source, on_error=None):
    """Return a session_reflect event dict, or None if classification
    failed/was empty. `on_error(context, exc)` is called (if given) on any
    exception so the caller can log it without this module knowing about
    the caller's error-log file."""
    if not excerpt:
        return None

    try:
        result = subprocess.run(
            ["claude", "-p", _prompt_for(excerpt), "--output-format", "text"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        if on_error:
            on_error("classify:subprocess", exc)
        return None

    out = result.stdout.strip()
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(out[start:end + 1])
    except Exception:
        return None

    task_type = parsed.get("task_type") if parsed.get("task_type") in VALID_TASK_TYPES else ""
    outcome = parsed.get("outcome") if parsed.get("outcome") in VALID_OUTCOMES else "unclear"

    return event_schema.make_event("session_reflect", session_id, source, {
        "tier": "advanced",
        "method": "llm_self_classify",
        "domain": str(parsed.get("domain", "general")).strip().lower()[:60],
        "topics": [str(t).strip().lower()[:40] for t in parsed.get("topics", [])][:5],
        "task_type": task_type,
        "outcome": outcome,
        "outcome_rationale": str(parsed.get("outcome_rationale", ""))[:120],
    })
