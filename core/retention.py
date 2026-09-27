"""Configurable retention (spec D3): delete evidence files older than N days.

The chosen retention period is persisted to a small config file so that
`prune` run with no argument (e.g. from a cron job or a future scheduled
hook) always applies the user's actual choice, not a hardcoded default.
"""

import datetime
import json
from pathlib import Path

from .local_storage import STORE_DIR

CONFIG_PATH = Path.home() / ".valuezen" / "propensity" / "config.json"
DEFAULT_RETENTION_DAYS = 90


def get_config():
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return {}


def update_config(**kwargs):
    cfg = get_config()
    cfg.update(kwargs)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def get_retention_days():
    return get_config().get("retention_days", DEFAULT_RETENTION_DAYS)


def set_retention_days(days):
    update_config(retention_days=days)


def prune(days=None):
    """Delete daily evidence files older than `days` (or the persisted default).

    Returns (removed_filenames, days_used).
    """
    days = days if days is not None else get_retention_days()
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    removed = []

    if STORE_DIR.exists():
        for fpath in sorted(STORE_DIR.glob("*.jsonl")):
            try:
                file_date = datetime.date.fromisoformat(fpath.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                fpath.unlink()
                removed.append(fpath.name)

    return removed, days
