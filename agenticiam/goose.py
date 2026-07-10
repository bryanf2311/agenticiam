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
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

OLLAMA_API_BASE = "http://127.0.0.1:11434"
ANTHROPIC_API_BASE = "https://api.anthropic.com"
GOOGLE_API_BASE = "https://generativelanguage.googleapis.com"

# Confirmed against block/goose's actual provider source (anthropic_def.rs,
# google_def.rs) and its provider docs table — not guessed. Note Gemini's
# env var is GOOGLE_API_KEY, not GEMINI_API_KEY, despite the product name.
PROVIDER_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


class ProviderUnavailable(Exception):
    pass


class OllamaUnavailable(ProviderUnavailable):
    pass


class DispatchError(Exception):
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


def list_anthropic_models(api_key: str, timeout: float = 5.0) -> list:
    if not api_key:
        raise ProviderUnavailable("an Anthropic API key is required to list models")
    req = urllib.request.Request(
        f"{ANTHROPIC_API_BASE}/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderUnavailable(str(exc)) from exc
    return sorted(m["id"] for m in data.get("data", []) if m.get("id"))


def list_google_models(api_key: str, timeout: float = 5.0) -> list:
    if not api_key:
        raise ProviderUnavailable("a Google API key is required to list models")
    url = f"{GOOGLE_API_BASE}/v1beta/models?key={urllib.parse.quote(api_key)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderUnavailable(str(exc)) from exc
    names = []
    for m in data.get("models", []):
        name = m.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        if name:
            names.append(name)
    return sorted(names)


def list_provider_models(provider: str, api_key: str = None, timeout: float = 5.0) -> list:
    if provider == "ollama":
        return list_ollama_models(timeout=timeout)
    if provider == "anthropic":
        return list_anthropic_models(api_key, timeout=timeout)
    if provider == "google":
        return list_google_models(api_key, timeout=timeout)
    raise ValueError(f"unknown provider {provider!r}")


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


def set_default_provider_model(config: dict, provider: str, model: str, context_limit: int = None) -> dict:
    config = dict(config)
    config["GOOSE_PROVIDER"] = provider
    config["GOOSE_MODEL"] = model
    if context_limit:
        config["GOOSE_CONTEXT_LIMIT"] = context_limit
        if provider == "ollama":
            # GOOSE_CONTEXT_LIMIT is goose's own context-management threshold;
            # GOOSE_INPUT_LIMIT is the one that actually reaches Ollama's
            # num_ctx, so both need setting to really change the window.
            config["GOOSE_INPUT_LIMIT"] = context_limit
    return config


def launch_commands(
    name: str, provider: str = None, model: str = None, context_limit: int = None, api_key: str = None
) -> dict:
    """Shell-specific commands to start an interactive Goose session for
    this agent.

    `goose session` has no --provider/--model flags (only `goose run`
    does — confirmed against a real Goose install after an earlier
    version of this wizard generated `session --provider ... --model
    ...` and it rejected them outright). GOOSE_PROVIDER/GOOSE_MODEL/
    GOOSE_CONTEXT_LIMIT/provider API keys are all documented as readable
    from the environment as well as config.yaml, so a per-invocation
    override goes through env vars instead — whose syntax differs enough
    across shells that we hand back all three rather than guess which one
    the user is in.

    The provider API key (if any) is never written to config.yaml or
    persisted anywhere by AgenticIAM — Goose's own docs warn against
    keeping API keys in plaintext config files, preferring env vars or its
    OS-keyring-backed secret store. It only ever exists in this one-time
    generated command.
    """
    base = f"goose session -n {name}"
    bash_parts, ps_parts, cmd_parts = [], [], []

    def add(key, value, quote=False):
        if quote:
            bash_parts.append(f"{key}='{value}'")
            ps_parts.append(f"$env:{key}='{value}'")
        else:
            bash_parts.append(f"{key}={value}")
            ps_parts.append(f'$env:{key}="{value}"')
        cmd_parts.append(f'set "{key}={value}"')

    if model:
        provider = provider or "ollama"
        add("GOOSE_PROVIDER", provider)
        add("GOOSE_MODEL", model)
    if context_limit:
        add("GOOSE_CONTEXT_LIMIT", context_limit)
        if (provider or "ollama") == "ollama":
            add("GOOSE_INPUT_LIMIT", context_limit)
    key_env = PROVIDER_API_KEY_ENV.get(provider)
    if key_env and api_key:
        add(key_env, api_key, quote=True)

    if not bash_parts:
        return {"bash": base, "powershell": base, "cmd": base}
    return {
        "bash": " ".join(bash_parts) + f" {base}",
        "powershell": "; ".join(ps_parts) + f"; {base}",
        "cmd": " && ".join(cmd_parts) + f" && {base}",
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


DEFAULT_DISPATCH_TIMEOUT = 300.0  # matches the "timeout" field register_extension writes for the extension itself


def run_agent_task(
    provider: str, model: str, task: str, timeout: float = DEFAULT_DISPATCH_TIMEOUT, goose_binary: str = None
) -> str:
    """Runs a single non-interactive task through Goose as a given
    provider/model and returns its text response — this is the "manager
    dispatches to a worker" mechanism.

    Deliberately `goose run`, not `goose session`: `run` is the one that
    actually accepts --provider/--model overrides (confirmed against a
    real install after `session` rejected those flags outright — see
    launch_commands's docstring for the same lesson). `--no-session`
    keeps it a one-shot call with no persisted session state to manage.

    This only ever runs against providers that need no API key (in
    practice, Ollama) — AgenticIAM never stores provider API keys (see
    launch_commands), so there is nowhere to pull an Anthropic/Google key
    back out of for a dispatch call the manager didn't just type in.
    Callers are expected to enforce that restriction before calling this;
    it isn't re-checked here since this function has no notion of "worker
    identity", just provider/model/task.

    The default timeout matches the extension's own configured MCP
    timeout (see register_extension) — dispatching used to time out
    internally at 120s while the extension itself was allowed 300s,
    meaning our own code was giving up before Goose's own allowance
    would have. Local-model inference plus whatever extension-loading
    Goose does on its end can legitimately take a while; callers can pass
    a longer timeout still for a known-slow task.
    """
    binary = goose_binary or find_goose_binary() or "goose"
    cmd = [binary, "run", "--no-session", "--provider", provider, "--model", model, "-t", task]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DispatchError(f"goose executable not found ({binary})") from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills the process and drains whatever output it had
        # already produced onto the TimeoutExpired exception itself — surface
        # that instead of a bare "it timed out", since that's the only way to
        # tell a hung tool call apart from inference that's just plain slow.
        partial_out = (getattr(exc, "stdout", None) or "").strip()
        partial_err = (getattr(exc, "stderr", None) or "").strip()
        detail = ""
        if partial_err:
            detail += f"\nstderr so far:\n{partial_err[-2000:]}"
        if partial_out:
            detail += f"\nstdout so far:\n{partial_out[-2000:]}"
        raise DispatchError(f"dispatched task timed out after {timeout}s{detail}") from exc
    if result.returncode != 0:
        raise DispatchError(result.stderr.strip() or f"goose run exited with status {result.returncode}")
    return result.stdout.strip()
