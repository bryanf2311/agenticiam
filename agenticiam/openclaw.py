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
"""

import os
import shutil
from pathlib import Path

from .goose import MANAGER_SYSTEM_PROMPT, slugify  # noqa: F401 (re-exported for callers)


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


def agent_add_command(name: str, provider: str, model: str) -> dict:
    """`openclaw agents add` invocation that creates the persona (workspace,
    session store, model) — the OpenClaw equivalent of Goose's `goose
    session -n <name>` / `goose run --recipe ...` launch command."""
    slug = slugify(name)
    model_ref = f"{provider}/{model}" if provider and model else model
    ws = workspace_path(name)
    cmd = f'openclaw agents add {slug} --model "{model_ref}" --non-interactive --workspace "{ws}"'
    return {"bash": cmd, "powershell": cmd, "cmd": cmd}


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
