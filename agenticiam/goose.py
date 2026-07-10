"""Integration with Goose (block/goose), a local AI agent CLI, and Ollama.

This talks to two things on the *same machine* AgenticIAM's server process
is running on: Goose's config.yaml (to register this directory as an MCP
extension with a freshly-issued, scoped API key) and Ollama's local HTTP
API (to list installed models for the wizard's model picker). It does not
reach across the network — if AgenticIAM's server is running somewhere
other than the machine you run `goose session` on, this feature isn't
applicable there.

Goose has no notion of a per-agent "profile": GOOSE_PROVIDER/GOOSE_MODEL
are global config.yaml settings shared by every session, while extensions
(MCP servers) are a keyed map so multiple AgenticIAM-backed agent
identities can coexist as separate, independently enabled/disabled
entries. See register_extension / set_default_provider_model.
"""

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

OLLAMA_API_BASE = "http://127.0.0.1:11434"


class OllamaUnavailable(Exception):
    pass


def find_goose_binary() -> str:
    return shutil.which("goose")


def find_ollama_binary() -> str:
    return shutil.which("ollama")


def list_ollama_models(timeout: float = 2.0) -> list:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API_BASE}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise OllamaUnavailable(str(exc)) from exc
    return sorted(m["name"] for m in data.get("models", []) if m.get("name"))


def _config_path_for(is_windows: bool, environ: dict) -> Path:
    if is_windows:
        base = environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "Block" / "goose" / "config" / "config.yaml"
    return Path.home() / ".config" / "goose" / "config.yaml"


def config_path() -> Path:
    return _config_path_for(os.name == "nt", os.environ)


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def save_config(config: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(f".yaml.bak-{int(time.time())}")
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
    return path


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "agent"


def register_extension(
    config: dict,
    extension_id: str,
    display_name: str,
    cmd: str,
    args: list,
    token: str,
    timeout_seconds: int = 300,
) -> dict:
    config = dict(config)
    extensions = dict(config.get("extensions") or {})
    extensions[extension_id] = {
        "name": display_name,
        "cmd": cmd,
        "args": list(args),
        "enabled": True,
        "envs": {"AGENTICIAM_TOKEN": token},
        "type": "stdio",
        "timeout": timeout_seconds,
    }
    config["extensions"] = extensions
    return config


def set_default_provider_model(config: dict, provider: str, model: str) -> dict:
    config = dict(config)
    config["GOOSE_PROVIDER"] = provider
    config["GOOSE_MODEL"] = model
    return config


def launch_commands(name: str, provider: str = None, model: str = None) -> dict:
    """Shell-specific commands to start an interactive Goose session for
    this agent.

    `goose session` has no --provider/--model flags (only `goose run`
    does — confirmed against a real Goose install after an earlier
    version of this wizard generated `session --provider ... --model
    ...` and it rejected them outright). GOOSE_PROVIDER/GOOSE_MODEL are
    documented as readable from the environment as well as config.yaml,
    so a per-invocation override goes through env vars instead — whose
    syntax differs enough across shells that we hand back all three
    rather than guess which one the user is in.
    """
    base = f"goose session -n {name}"
    if not model:
        return {"bash": base, "powershell": base, "cmd": base}
    provider = provider or "ollama"
    return {
        "bash": f"GOOSE_PROVIDER={provider} GOOSE_MODEL={model} {base}",
        "powershell": f'$env:GOOSE_PROVIDER="{provider}"; $env:GOOSE_MODEL="{model}"; {base}',
        "cmd": f'set "GOOSE_PROVIDER={provider}" && set "GOOSE_MODEL={model}" && {base}',
    }


def extension_snippet_yaml(
    extension_id: str, display_name: str, cmd: str, args: list, token: str, timeout_seconds: int = 300
) -> str:
    """Rendered standalone so it can be shown to the user to paste in by
    hand if writing the real config file fails (permissions, read-only FS, ...)."""
    return yaml.safe_dump(
        {
            "extensions": {
                extension_id: {
                    "name": display_name,
                    "cmd": cmd,
                    "args": list(args),
                    "enabled": True,
                    "envs": {"AGENTICIAM_TOKEN": token},
                    "type": "stdio",
                    "timeout": timeout_seconds,
                }
            }
        },
        default_flow_style=False,
        sort_keys=False,
    )
