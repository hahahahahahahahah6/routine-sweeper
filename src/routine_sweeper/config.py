"""Configuration for routine-sweeper.

Defaults live in ``~/.claude/routine-sweeper.json``; every CLI flag overrides
the file. The file is never written by the tool -- create it yourself if you
want machine-wide defaults.
"""

import json
import os

DEFAULTS = {
    "keep": 10,                  # newest runs kept per routine
    "older_than_days": None,     # additionally require this age for deletion
    "include_engaged": False,    # also sweep runs a human replied in
    "live_window_minutes": 15,   # runs active this recently are never touched
    "whitelist": [],             # routine names / session ids / fnmatch patterns
}


def load(claude_dir=None):
    cfg = dict(DEFAULTS)
    path = os.path.join(
        os.path.expanduser("~"), ".claude", "routine-sweeper.json"
    )
    if claude_dir is not None:
        path = os.path.join(claude_dir, "routine-sweeper.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return cfg
    if isinstance(data, dict):
        for key in DEFAULTS:
            if key in data:
                cfg[key] = data[key]
    return cfg
