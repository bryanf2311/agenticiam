"""Resolves where AgenticIAM keeps its state (directory DB, signing key).

Mirrors the "one domain controller, one data store" model of Active
Directory: everything lives under a single data directory, overridable via
AGENTICIAM_HOME for tests or multi-instance setups.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("AGENTICIAM_HOME")
    if override:
        path = Path(override)
    elif os.name == "nt":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        path = Path(base) / "AgenticIAM"
    else:
        base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        path = Path(base) / "agenticiam"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "directory.db"


def signing_key_path() -> Path:
    return data_dir() / "signing.key"


def telegram_bots_path() -> Path:
    """Display names for OpenClaw Telegram bot accounts — AgenticIAM's own
    bookkeeping, not OpenClaw's: `openclaw channels list --json` returns
    bare account ids with no name field (confirmed against real output),
    so there's nothing to read a friendly name back from OpenClaw itself."""
    return data_dir() / "openclaw-telegram-bots.yaml"
