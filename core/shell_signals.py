"""Shared, adapter-agnostic heuristics for reading shell-command and
file-edit metadata — used by any adapter whose client runs Bash/exec
commands and patches files (claude-code, codex). Purely structural: a
language guess from a file extension, and "did this look like a
test-runner invocation, did it exit cleanly" — never a parsed test count,
never command output content.
"""

import re
from pathlib import Path

LANGUAGE_BY_EXT = {
    "py": "python", "js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
    "go": "go", "rs": "rust", "java": "java", "rb": "ruby", "c": "c", "cpp": "c++", "h": "c",
    "sh": "shell", "sql": "sql", "md": "markdown", "json": "json", "yaml": "yaml", "yml": "yaml",
    "css": "css", "html": "html", "swift": "swift", "kt": "kotlin", "php": "php",
}

# Best-effort: does this shell command *contain* a test-runner invocation
# anywhere in it? Real usage is rarely a bare `pytest` — it's typically
# `cd backend && source venv/bin/activate && python -m pytest ...`, so this
# searches the whole (possibly multi-line) command rather than anchoring to
# the start. Not exhaustive — this is a coarse signal, not a build-system
# integration.
TEST_CMD_RE = re.compile(
    r"\b(?:python3?\s+-m\s+)?(pytest|py\.test|npm\s+(?:run\s+)?test\b|yarn\s+test\b|jest\b|go\s+test\b|"
    r"cargo\s+test\b|mvn\s+test\b|gradle\s+test\b|rspec\b|mocha\b|dotnet\s+test\b)", re.IGNORECASE,
)


def language_for(file_path):
    ext = Path(file_path).suffix.lstrip(".").lower()
    return LANGUAGE_BY_EXT.get(ext, ext or None)


def test_run_for_command(command, is_error):
    """Is this shell command a test-runner invocation? If so, return
    (invocations=1, clean_exit=0-or-1) — a purely structural signal (did a
    test-runner command run, did it exit 0), never a parsed test count.
    Returns None if this doesn't look like a test-runner invocation at all.

    Excludes lines that merely *mention* a test runner (grepping for it,
    checking if it's still running) rather than invoking one — a command
    containing e.g. `grep pytest` or `ps aux | grep pytest` would otherwise
    match TEST_CMD_RE on the word "pytest" alone."""
    if not command:
        return None
    real_invocation = any(
        TEST_CMD_RE.search(ln) and "grep" not in ln.lower() and not ln.strip().startswith(("ps ", "while "))
        for ln in command.splitlines()
    )
    if not real_invocation:
        return None
    return (1, 0) if is_error else (1, 1)
