"""Integration with OpenClaw (openclaw/openclaw), a self-hosted multi-channel
gateway that connects chat apps to AI agents.

Unlike Goose, we never write to OpenClaw's own config file directly: it's
JSON5 (comments, trailing commas) which Python's stdlib `json` can't
round-trip safely, and OpenClaw ships a purpose-built CLI for exactly this
job (`openclaw mcp add`, `openclaw agents add`) that handles the config
file safely on our behalf. So this module only *generates commands* for
the user to run, plus (for the one piece that's a plain text file, not
JSON5) writes a manager agent's `SOUL.md` directly, mirroring how
goose.write_manager_recipe works.

Confirmed against OpenClaw's own docs (docs.openclaw.ai / the openclaw/openclaw
GitHub docs source), not guessed:
- Install: openclaw.ai/install.sh (macOS/Linux) or install.ps1 (Windows).
- `openclaw mcp add <name> --command <cmd> --arg <a> --env KEY=VALUE` registers
  a stdio MCP server under `mcp.servers.<name>` (command/args/env schema).
- `openclaw agents add <id> --model <provider>/<model> --non-interactive
  --workspace <dir>` creates an isolated agent persona; `--non-interactive`
  requires `--workspace`. Each agent's workspace holds a `SOUL.md` file that
  defines its persona/system prompt.
- Custom model providers (including Ollama Cloud, which isn't one of
  OpenClaw's built-in provider ids) go under `models.providers.<id>` —
  `baseUrl`/`apiKey`/`api` fields, `api: "openai-completions"` for
  self-hosted /v1/chat/completions-shaped backends (which is what
  ollama.com's hosted API is) — set non-destructively via
  `openclaw config set models.providers.<id> '<json>' --strict-json --merge`.
"""

import json
import os
import shutil
from pathlib import Path

from .goose import MANAGER_SYSTEM_PROMPT, slugify  # noqa: F401 (re-exported for callers)

OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
OLLAMA_CLOUD_PROVIDER_ID = "ollama-cloud"


def find_openclaw_binary() -> str:
    return shutil.which("openclaw")


def install_commands() -> dict:
    """One-time OpenClaw install + onboarding + verification commands."""
    return {
        "bash": (
            "curl -fsSL https://openclaw.ai/install.sh | bash && "
            "openclaw onboard --install-daemon && "
            "openclaw gateway status"
        ),
        "powershell": (
            "iwr -useb https://openclaw.ai/install.ps1 | iex; "
            "openclaw onboard --install-daemon; "
            "openclaw gateway status"
        ),
        "cmd": (
            "REM OpenClaw's installer is a PowerShell script; run this from PowerShell, not cmd.exe:\n"
            "REM iwr -useb https://openclaw.ai/install.ps1 | iex"
        ),
    }


def _workspace_path_for(slug: str, is_windows: bool, environ: dict) -> Path:
    home = Path(environ.get("USERPROFILE" if is_windows else "HOME", str(Path.home())))
    return home / ".openclaw" / f"workspace-{slug}"


def workspace_path(name: str) -> Path:
    return _workspace_path_for(slugify(name), os.name == "nt", os.environ)


def mcp_add_commands(name: str, cmd: str, args: list, token: str) -> dict:
    """`openclaw mcp add` invocation that registers this identity's
    AgenticIAM MCP tools with OpenClaw — the OpenClaw equivalent of Goose's
    `register_extension` config.yaml write."""
    slug = slugify(name)
    arg_flags = " ".join(f'--arg "{a}"' for a in args)

    def build(quoted_token):
        return f'openclaw mcp add {slug} --command "{cmd}" {arg_flags} --env AGENTICIAM_TOKEN={quoted_token}'

    return {
        "bash": build(f"'{token}'"),
        "powershell": build(f"'{token}'"),
        "cmd": build(f'"{token}"'),
    }


# AgenticIAM/Goose call this provider "ollama_cloud" (matching Goose's own
# internal provider id); the custom provider we register with OpenClaw
# under models.providers uses OLLAMA_CLOUD_PROVIDER_ID ("ollama-cloud")
# instead, since OpenClaw has no built-in provider of that name.
_PROVIDER_ID_OVERRIDES = {"ollama_cloud": OLLAMA_CLOUD_PROVIDER_ID}


def agent_add_command(name: str, provider: str, model: str) -> dict:
    """`openclaw agents add` invocation that creates the persona (workspace,
    session store, model) — the OpenClaw equivalent of Goose's `goose
    session -n <name>` / `goose run --recipe ...` launch command."""
    slug = slugify(name)
    openclaw_provider = _PROVIDER_ID_OVERRIDES.get(provider, provider)
    model_ref = f"{openclaw_provider}/{model}" if openclaw_provider and model else model
    ws = workspace_path(name)
    cmd = f'openclaw agents add {slug} --model "{model_ref}" --non-interactive --workspace "{ws}"'
    return {"bash": cmd, "powershell": cmd, "cmd": cmd}


def ollama_cloud_provider_command(api_key: str, model: str = None) -> dict:
    """`openclaw config set models.providers.<id> ...` invocation that
    registers Ollama Cloud as a custom OpenClaw model provider (there's no
    OpenClaw built-in provider for it) — the OpenClaw equivalent of Goose's
    one-time `goose configure` step for the same thing. Unlike Goose's
    path, this one *is* fully scriptable: OpenClaw's custom-provider config
    is plain JSON reachable through its own `config set --merge` CLI, not a
    keyring-backed declarative provider file."""
    payload = {"baseUrl": OLLAMA_CLOUD_BASE_URL, "apiKey": api_key, "api": "openai-completions"}
    if model:
        payload["models"] = [{"id": model}]
    json_str = json.dumps(payload)
    bash_cmd = f"openclaw config set models.providers.{OLLAMA_CLOUD_PROVIDER_ID} '{json_str}' --strict-json --merge"
    cmd_json = json_str.replace('"', '\\"')
    cmd_cmd = f'openclaw config set models.providers.{OLLAMA_CLOUD_PROVIDER_ID} "{cmd_json}" --strict-json --merge'
    return {"bash": bash_cmd, "powershell": bash_cmd, "cmd": cmd_cmd}


def manager_soul_md(name: str) -> str:
    """SOUL.md content for a manager agent — same dispatch guide used for
    Goose's manager recipe (see goose.MANAGER_SYSTEM_PROMPT), since OpenClaw's
    SOUL.md plays the same "persona/system prompt" role Goose's recipe
    `instructions` field does."""
    return f"# {name} — Manager\n\n{MANAGER_SYSTEM_PROMPT}"


def write_manager_soul(name: str) -> Path:
    ws = workspace_path(name)
    ws.mkdir(parents=True, exist_ok=True)
    path = ws / "SOUL.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(manager_soul_md(name))
    return path
