"""Integration with OpenClaw (openclaw/openclaw), a self-hosted multi-channel
gateway that connects chat apps to AI agents.

Unlike Goose, we never write to OpenClaw's own config file directly: it's
JSON5 (comments, trailing commas) which Python's stdlib `json` can't
round-trip safely, and OpenClaw ships a purpose-built CLI for exactly this
job (`openclaw mcp add`, `openclaw agents add`, `openclaw config get/set`)
that handles the config file safely on our behalf. Two call shapes live
here side by side: functions that *generate commands* for the user to run
themselves (one-time setup — install, MCP registration, agent creation),
and functions that *run the CLI directly* as a subprocess on this same
machine (list_agents, get_agent_config, set_agent_*, get/set_website_
allowlist) for live agent visibility and permission management, the same
way goose.run_agent_task shells out directly for dispatch. Both rely on
the CLI, never on parsing/writing openclaw.json ourselves. The one plain
text file involved, a manager agent's `SOUL.md`, is written directly,
mirroring goose.write_manager_recipe.

Confirmed against OpenClaw's own docs (docs.openclaw.ai / the openclaw/openclaw
GitHub docs source), not guessed:
- Install: openclaw.ai/install.sh (macOS/Linux) or install.ps1 (Windows).
- `openclaw mcp add <name> --command <cmd> --arg <a> --env KEY=VALUE` registers
  a stdio MCP server under `mcp.servers.<name>` (command/args/env schema).
- `openclaw agents add <id> --model <provider>/<model> --non-interactive
  --workspace <dir>` creates an isolated agent persona; `--non-interactive`
  requires `--workspace`. Each agent's workspace holds a `SOUL.md` file that
  defines its persona/system prompt.
- `openclaw agents list --json` lists every agent OpenClaw knows about.
- `openclaw config get '<path>' --json` / `config set '<path>' '<json>'
  --strict-json [--merge]` read/write arbitrary config paths; array
  elements are addressed by index, e.g. `agents.list[0].tools.allow`.
- Per-agent permissions live under `agents.list[<idx>].tools.allow`/`.deny`
  (tool-level gating — file tools, the `browser` tool, etc.) and
  `agents.list[<idx>].sandbox.*` (docker.binds for filesystem paths,
  workspaceAccess, docker.network) — see TOOL_CATALOG and set_agent_*.
- Website/domain restriction (`browser.ssrfPolicy.hostnameAllowlist`) is
  GLOBAL only — OpenClaw has no per-agent domain allowlist, only a
  per-agent on/off toggle for the `browser` tool itself.
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
import subprocess
from pathlib import Path

from .goose import MANAGER_SYSTEM_PROMPT, slugify  # noqa: F401 (re-exported for callers)

OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
OLLAMA_CLOUD_PROVIDER_ID = "ollama-cloud"

DEFAULT_CLI_TIMEOUT = 15.0

# Real, confirmed-against-docs tool ids OpenClaw's `agents.list[].tools.allow`
# / `.deny` gate applies to — grouped for the permissions UI. "Web access"
# gating the `browser` tool is deliberately coarse (on/off): OpenClaw has no
# per-agent website/domain allowlist, only a *global* one (see
# get_website_allowlist/set_website_allowlist below, under
# browser.ssrfPolicy.hostnameAllowlist) shared by every agent with browser
# access enabled.
TOOL_CATALOG = {
    "File access": ["read", "write", "edit", "process", "bash"],
    "Web access": ["browser"],
    "Other": ["cron", "discord", "gateway", "canvas", "nodes", "sessions_list", "sessions_history", "sessions_send", "sessions_spawn"],
}


class OpenClawCliError(Exception):
    pass


class OpenClawTimeoutError(OpenClawCliError):
    """Raised specifically for a subprocess.TimeoutExpired, as opposed to a
    real CLI error — callers doing a mutation (_set_agent_path,
    set_website_allowlist) catch just this to attempt a read-back check
    (see those functions' docstrings for why: a real field report showed
    this exact openclaw build finishing its work and printing complete
    output well within the timeout, but never exiting the process on its
    own — see _run_openclaw's docstring)."""


def find_openclaw_binary() -> str:
    return shutil.which("openclaw")


def _run_openclaw(
    args: list, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT, recover_json_on_timeout: bool = False
) -> str:
    """Runs an `openclaw` subcommand and returns its stdout as a string.

    Mirrors goose.run_agent_task's subprocess handling (same CREATE_NO_WINDOW
    reasoning on Windows, same defensive-against-None-stdout guard learned
    the hard way from a real field report against that function).

    A second, distinct field report: `openclaw agents list --json` reported
    as "timed out after 15.0s" even though the timeout's own partial-output
    capture showed the CLI had already printed a complete, well-formed JSON
    array — the work was done and correct well inside the timeout. The
    process itself just never exited on its own (a lingering connection to
    the gateway, going by the stray "[state-migrations]" warning in this
    build, keeping its event loop alive past the point its actual job was
    finished) — a real quirk of this specific openclaw build/version, not
    something AgenticIAM's subprocess call is doing wrong. subprocess.run
    can only tell a genuine hang from "done but won't exit" by whether
    complete output was produced, so `recover_json_on_timeout` (used by
    _run_openclaw_json, the read-only paths) parses whatever partial stdout
    the timeout captured and treats successfully-parsed JSON as the real
    result rather than a failure — see also OpenClawTimeoutError for the
    read-back-and-verify fallback mutations use for the same underlying
    issue.
    """
    binary = openclaw_binary or find_openclaw_binary() or "openclaw"
    cmd = [binary, *args]
    # stdin=DEVNULL: a real field report showed `openclaw agents list --json`
    # hanging until timeout with the gateway running. Left unset, a child
    # process on some platforms inherits the parent's stdin; if the CLI ever
    # falls back to an interactive prompt (wrong/unrecognized flag, a
    # confirmation dialog, etc.) it would block reading from that instead of
    # failing fast. Closing stdin up front turns "hangs forever" into
    # "fails immediately with whatever error the CLI gives an unattended
    # caller" — a real, diagnosable error instead of a bare timeout.
    popen_kwargs = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **popen_kwargs)
    except FileNotFoundError as exc:
        raise OpenClawCliError(f"openclaw executable not found ({binary})") from exc
    except subprocess.TimeoutExpired as exc:
        partial_out = (getattr(exc, "stdout", None) or "").strip()
        partial_err = (getattr(exc, "stderr", None) or "").strip()
        if recover_json_on_timeout and partial_out:
            try:
                json.loads(partial_out)
            except ValueError:
                pass
            else:
                return partial_out
        detail = ""
        if partial_err:
            detail += f"\nstderr so far:\n{partial_err[-2000:]}"
        if partial_out:
            detail += f"\nstdout so far:\n{partial_out[-2000:]}"
        raise OpenClawTimeoutError(f"openclaw {' '.join(args)} timed out after {timeout}s{detail}") from exc
    if result.returncode != 0:
        raise OpenClawCliError((result.stderr or "").strip() or f"openclaw {' '.join(args)} exited with status {result.returncode}")
    return result.stdout or ""


def _run_openclaw_json(args: list, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT):
    out = _run_openclaw(args, openclaw_binary=openclaw_binary, timeout=timeout, recover_json_on_timeout=True).strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError as exc:
        raise OpenClawCliError(f"openclaw {' '.join(args)} produced non-JSON output: {out[:200]!r}") from exc


def list_agents(openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> list:
    """Every agent OpenClaw currently knows about — not just ones AgenticIAM
    created — via `openclaw agents list --json`."""
    data = _run_openclaw_json(["agents", "list", "--json"], openclaw_binary=openclaw_binary, timeout=timeout)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return data.get("agents", [])


def _agent_index(agent_id: str, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> int:
    for i, agent in enumerate(list_agents(openclaw_binary=openclaw_binary, timeout=timeout)):
        if agent.get("id") == agent_id:
            return i
    raise OpenClawCliError(f"no OpenClaw agent with id {agent_id!r}")


def get_agent_config(agent_id: str, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> dict:
    """Full config subtree (model, workspace, tools.allow/deny, sandbox...)
    for one agent, via `openclaw config get 'agents.list[<idx>]' --json`."""
    idx = _agent_index(agent_id, openclaw_binary=openclaw_binary, timeout=timeout)
    data = _run_openclaw_json(
        ["config", "get", f"agents.list[{idx}]", "--json"], openclaw_binary=openclaw_binary, timeout=timeout
    )
    return data or {}


def _config_set_verified(path: str, value, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> None:
    """`config set <path> <value> --strict-json`, with a fallback for the
    same "finished the write but the process wouldn't exit" behavior
    _run_openclaw's recover_json_on_timeout works around for reads (see its
    docstring) — except a `config set` prints no output to recover from, so
    there's nothing to parse out of the timeout. Instead, on a timeout,
    read the path straight back and compare: if the value's already there,
    the write genuinely succeeded despite the CLI call looking like a
    failure; if not, it's a real timeout and the error is re-raised."""
    try:
        _run_openclaw(
            ["config", "set", path, json.dumps(value), "--strict-json"], openclaw_binary=openclaw_binary, timeout=timeout
        )
    except OpenClawTimeoutError:
        current = _run_openclaw_json(["config", "get", path, "--json"], openclaw_binary=openclaw_binary, timeout=timeout)
        if current != value:
            raise


def _set_agent_path(
    agent_id: str, path_suffix: str, value, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> None:
    idx = _agent_index(agent_id, openclaw_binary=openclaw_binary, timeout=timeout)
    path = f"agents.list[{idx}].{path_suffix}"
    _config_set_verified(path, value, openclaw_binary=openclaw_binary, timeout=timeout)


def set_agent_tools(
    agent_id: str, allow: list = None, deny: list = None, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> None:
    """Replaces agents.list[<idx>].tools.allow and/or .deny wholesale — the
    UI sends the complete desired list each time, not a delta, so a plain
    (non-merge) `config set` on the specific leaf path is correct: it
    replaces just that array without touching sibling tools.* fields."""
    if allow is not None:
        _set_agent_path(agent_id, "tools.allow", allow, openclaw_binary=openclaw_binary, timeout=timeout)
    if deny is not None:
        _set_agent_path(agent_id, "tools.deny", deny, openclaw_binary=openclaw_binary, timeout=timeout)


def set_agent_filesystem_binds(
    agent_id: str, binds: list, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> None:
    """Replaces agents.list[<idx>].sandbox.docker.binds — "host:container:ro"
    / "host:container:rw" strings — OpenClaw's actual per-path filesystem
    access control. Only takes effect while that agent is sandboxed (docker
    backend); outside a sandbox there's no per-path allowlist to set."""
    _set_agent_path(agent_id, "sandbox.docker.binds", binds, openclaw_binary=openclaw_binary, timeout=timeout)


def set_agent_sandbox(
    agent_id: str,
    mode: str = None,
    workspace_access: str = None,
    network: str = None,
    openclaw_binary: str = None,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> None:
    """mode: off | non-main | all. workspace_access: none | ro | rw.
    network: "none" to cut the sandboxed agent off from the network
    entirely, or omit/None to leave the default (allowed) in place."""
    if mode is not None:
        _set_agent_path(agent_id, "sandbox.mode", mode, openclaw_binary=openclaw_binary, timeout=timeout)
    if workspace_access is not None:
        _set_agent_path(agent_id, "sandbox.workspaceAccess", workspace_access, openclaw_binary=openclaw_binary, timeout=timeout)
    if network is not None:
        _set_agent_path(agent_id, "sandbox.docker.network", network, openclaw_binary=openclaw_binary, timeout=timeout)


def get_website_allowlist(openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> list:
    """browser.ssrfPolicy.hostnameAllowlist — GLOBAL, not per-agent: OpenClaw
    has no per-agent website/domain allowlist. Every agent with the
    `browser` tool enabled (see TOOL_CATALOG / set_agent_tools) shares this
    same list."""
    data = _run_openclaw_json(
        ["config", "get", "browser.ssrfPolicy.hostnameAllowlist", "--json"], openclaw_binary=openclaw_binary, timeout=timeout
    )
    return data or []


def set_website_allowlist(hostnames: list, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> None:
    _config_set_verified(
        "browser.ssrfPolicy.hostnameAllowlist", hostnames, openclaw_binary=openclaw_binary, timeout=timeout
    )


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
