"""Key-health surface (P3, issue #16): last-backup-verified timestamp.

Tiny JSON state file next to gnostr.db. It stores NO key material — only the
unix timestamp of the last time the user produced (or re-confirmed) a backup.
Settings shows the timestamp plus a re-verify prompt.
"""

import json
import os
import time

from gi.repository import GLib

STATE_FILENAME = "key_health.json"


def _state_path():
    return os.path.join(GLib.get_user_data_dir(), "gnostr", STATE_FILENAME)


def mark_backup_verified():
    """Record that the user just produced/confirmed a backup. Returns the unix ts."""
    ts = int(time.time())
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_backup_verified": ts}, f)
    return ts


def last_backup_verified():
    """Unix timestamp of the last verified backup, or None if never."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            value = json.load(f).get("last_backup_verified")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError):
        return None


def format_last_backup(ts):
    """Human-readable local time for Settings copy."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
