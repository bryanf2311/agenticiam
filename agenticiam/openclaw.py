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
- `openclaw channels add --channel telegram --account <id> --name <name>
  --token <token>` registers a named bot token non-interactively and —
  per OpenClaw's own docs — asks a reachable local Gateway to start the
  account immediately, no restart needed; re-running it against the same
  --account id updates the token in place (confirmed: `channels add
  --help` describes the command itself as "Add or update a channel
  account"). (Not `--token-file`: that persists as a `tokenFile` config
  pointer, not a one-time read, and takes precedence over `botToken` even
  when stale — see add_or_update_telegram_bot's docstring for the real
  field report.) `channels.telegram.dmPolicy` ("pairing" by default: the
  bot owner can use it right away, anyone else needs a one-time
  `openclaw pairing approve telegram <code>`; "open" with
  `allowFrom: ["*"]` skips that approval for everyone) is global — shared
  by every bot account, not per-account — handled by
  set_telegram_dm_policy.
- Every account is explicitly named — there's deliberately no implicit
  "default account" path left anywhere in this module: an earlier version
  omitted --account (falling back to OpenClaw's own "default"), which
  caused a real, reported problem (silently colliding with/overwriting
  whatever else was using that account). `openclaw agents bind --agent
  <id> --bind telegram:<account>` routes one specific bot's traffic to a
  specific, already-existing agent; `openclaw agents add ... --bind
  telegram:<account>` does the same thing at creation time in one step,
  for a brand new agent. Never `telegram:*` (every account) from this
  module's own call sites, for the same reason.
"""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import yaml

from . import paths
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


def _kill_process_tree(pid: int) -> None:
    """Kills pid and everything spawned under it, not just pid itself.

    A real field report: after the timeout-and-recover workaround below
    started shipping, openclaw CLI invocations were still piling up as
    orphaned processes in Task Manager. subprocess.run's own timeout
    handling only calls .kill() on the one process it started directly —
    on Windows that's TerminateProcess on that single PID, on POSIX a
    plain SIGKILL to that one PID. If openclaw (a Node CLI reaching for
    a gateway process, per the "[state-migrations]"/never-exits behavior
    documented on _run_openclaw) spawns anything else along the way,
    killing just the top-level PID leaves that "anything else" running
    forever. `taskkill /T` walks the real Windows process tree by
    parent-child PID, killing every descendant; killpg needs the child
    started in its own session (see start_new_session below) so its pgid
    equals its pid and the whole group goes down together.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


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
    something AgenticIAM's subprocess call is doing wrong. `recover_json_on_
    timeout` (used by _run_openclaw_json, the read-only paths) parses
    whatever partial stdout the timeout captured and treats successfully-
    parsed JSON as the real result rather than a failure — see also
    OpenClawTimeoutError for the read-back-and-verify fallback mutations
    use for the same underlying issue.

    Uses Popen directly, not subprocess.run: a third field report showed
    that "quirk" piling up as orphaned processes in Task Manager over
    normal use of this app — subprocess.run's built-in timeout handling
    can only kill the one process it started, not anything that process
    spawned, so this explicitly kills the whole tree instead (see
    _kill_process_tree).
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
    else:
        # Own session so the child's pgid equals its pid — required for
        # _kill_process_tree's os.killpg to reach the whole tree rather
        # than just this one process.
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen_kwargs)
    except FileNotFoundError as exc:
        raise OpenClawCliError(f"openclaw executable not found ({binary})") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        partial_out = (stdout or "").strip()
        partial_err = (stderr or "").strip()
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
        raise OpenClawTimeoutError(f"openclaw {' '.join(args)} timed out after {timeout}s{detail}")
    if proc.returncode != 0:
        raise OpenClawCliError((stderr or "").strip() or f"openclaw {' '.join(args)} exited with status {proc.returncode}")
    return stdout or ""


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


TELEGRAM_DM_POLICIES = ("pairing", "open")


def set_telegram_dm_policy(
    dm_policy: str, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> None:
    """dm_policy="pairing" (OpenClaw's own default) means only the bot
    owner can use a Telegram bot immediately; anyone else needs a one-time
    `openclaw pairing approve telegram <code>`. dm_policy="open" also sets
    allowFrom: ["*"] so nobody needs approval — see docs.openclaw.ai/
    channels/telegram for the tradeoff. This is genuinely global — one
    setting shared by every bot account, not per-account — so it's kept
    separate from add_or_update_telegram_bot rather than folded into it.
    """
    if dm_policy not in TELEGRAM_DM_POLICIES:
        raise ValueError(f"dm_policy must be one of {TELEGRAM_DM_POLICIES}")
    if dm_policy == "open":
        _config_set_verified("channels.telegram.dmPolicy", "open", openclaw_binary=openclaw_binary, timeout=timeout)
        _config_set_verified("channels.telegram.allowFrom", ["*"], openclaw_binary=openclaw_binary, timeout=timeout)
    else:
        _config_set_verified("channels.telegram.dmPolicy", "pairing", openclaw_binary=openclaw_binary, timeout=timeout)


def get_telegram_status(openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> dict:
    """Whether Telegram is configured at all, and its current dmPolicy —
    read straight from `channels.telegram`, the same path
    set_telegram_dm_policy writes to. Surfacing this (rather than only
    showing it right after a successful connect) matters because a bot
    can sit fully configured with dmPolicy="pairing" and nobody's pending
    approvals ever get checked — the UI otherwise gives no hint that a
    "no reply" report might just mean the sender was never approved."""
    data = _run_openclaw_json(["config", "get", "channels.telegram", "--json"], openclaw_binary=openclaw_binary, timeout=timeout)
    data = data or {}
    configured = bool(data.get("enabled")) or bool(data.get("botToken"))
    return {"configured": configured, "dm_policy": data.get("dmPolicy") or "pairing"}


# ---------------------------------------------------------------- multiple Telegram bots
#
# OpenClaw supports more than one Telegram bot at once, each its own
# "account" under channels.telegram.accounts.<id>, independently bindable
# to a different agent (confirmed: `openclaw channels add --help` shows
# --account/--name/--token; a real run of `openclaw channels list --json`
# returned `{"chat": {"telegram": {"accounts": ["default"], "installed":
# true, "origin": "configured"}}}` — bare account-id strings, no name or
# token surfaced; `openclaw channels remove --help` confirms --account
# and --delete). AgenticIAM keeps the human-friendly display name itself
# (telegram_bots_path, next to goose's secrets.yaml in spirit) since
# OpenClaw's own CLI has nowhere confirmed to read one back from.


def load_telegram_bot_names() -> dict:
    path = paths.telegram_bots_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def save_telegram_bot_names(names: dict) -> Path:
    path = paths.telegram_bots_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(f".yaml.bak-{int(time.time())}")
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(names, f, default_flow_style=False, sort_keys=False)
    return path


def list_telegram_bots(openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> list:
    """Every configured Telegram bot account: id (OpenClaw's) + display
    name (AgenticIAM's own bookkeeping, falling back to the id itself for
    an account that exists in OpenClaw but was never named through here —
    e.g. the "default" account from the single-bot Telegram flow)."""
    data = _run_openclaw_json(["channels", "list", "--json"], openclaw_binary=openclaw_binary, timeout=timeout) or {}
    account_ids = ((data.get("chat") or {}).get("telegram") or {}).get("accounts") or []
    names = load_telegram_bot_names()
    return [{"id": account_id, "name": names.get(account_id, account_id)} for account_id in account_ids]


def add_or_update_telegram_bot(
    name: str, token: str, account_id: str = None, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> str:
    """Registers a new named Telegram bot, or — running this again with
    the same account_id — replaces its token in place. Confirmed via
    `openclaw channels add --help`: the command's own description is
    "Add or update a channel account", and its telegram example is
    literally captioned "Add or update Telegram non-interactively" — no
    separate rotate/update command exists or is needed. Returns the
    account id (slugified from name if not given explicitly, so the same
    name always maps back to the same account for a later update)."""
    account_id = account_id or slugify(name)
    _run_openclaw(
        ["channels", "add", "--channel", "telegram", "--account", account_id, "--name", name, "--token", token],
        openclaw_binary=openclaw_binary, timeout=timeout,
    )
    names = load_telegram_bot_names()
    names[account_id] = name
    save_telegram_bot_names(names)
    return account_id


def remove_telegram_bot(account_id: str, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT) -> None:
    """Deletes a Telegram bot account entirely. --delete is required: per
    `openclaw channels remove --help`, omitting it instead asks
    interactively whether to just disable the account — which would hang
    a non-interactive caller waiting on a prompt nobody can answer."""
    _run_openclaw(
        ["channels", "remove", "--channel", "telegram", "--account", account_id, "--delete"],
        openclaw_binary=openclaw_binary, timeout=timeout,
    )
    names = load_telegram_bot_names()
    if account_id in names:
        del names[account_id]
        save_telegram_bot_names(names)


def bind_agent_to_telegram_account(
    agent_id: str, account_id: str, openclaw_binary: str = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> None:
    """Routes just one Telegram bot's traffic to a specific agent —
    `--bind <channel>:<accountId>` (confirmed against docs/cli/agents.md).
    Never `telegram:*` (every account) — see this module's docstring for
    why. Other bots' routing is untouched."""
    _run_openclaw(
        ["agents", "bind", "--agent", agent_id, "--bind", f"telegram:{account_id}"],
        openclaw_binary=openclaw_binary, timeout=timeout,
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


def agent_add_command(name: str, provider: str, model: str, telegram_account_id: str = None) -> dict:
    """`openclaw agents add` invocation that creates the persona (workspace,
    session store, model) — the OpenClaw equivalent of Goose's `goose
    session -n <name>` / `goose run --recipe ...` launch command.

    telegram_account_id appends `--bind telegram:<account_id>` (a specific
    bot, never a `telegram:*` wildcard — binding every account to a brand
    new agent silently steals routing from any other bot already pointed
    elsewhere, a real problem this used to cause): since this command is
    the one the user actually runs to bring the new agent into existence
    (still copy-paste — OpenClaw agent creation isn't run directly the way
    Telegram bot registration is; see add_or_update_telegram_bot), it's
    also the earliest point a not-yet-existing agent *can* be bound to a
    channel. add_or_update_telegram_bot already made the named bot live,
    routed to OpenClaw's default agent, by the time this command gets
    run; running it hands routing for that one bot over to this new agent
    in the same step, with no separate `agents bind` call needed."""
    slug = slugify(name)
    openclaw_provider = _PROVIDER_ID_OVERRIDES.get(provider, provider)
    model_ref = f"{openclaw_provider}/{model}" if openclaw_provider and model else model
    ws = workspace_path(name)
    cmd = f'openclaw agents add {slug} --model "{model_ref}" --non-interactive --workspace "{ws}"'
    if telegram_account_id:
        cmd += f" --bind telegram:{telegram_account_id}"
    return {"bash": cmd, "powershell": cmd, "cmd": cmd}


# Real field report: OpenClaw's config write is Zod-validated, and
# models.providers.<id>.models is an array of objects, not bare strings —
# each one needs at least `id` and `name` or the CLI rejects the whole
# `config set` with "Config validation failed: ...models.0.name: Invalid
# input". `contextWindow` isn't enforced by that same validation error but
# is documented as needing to be >= 16000 (recommended >= 65536) for the
# gateway's own tooling not to auto-block the model, so it's set here too
# rather than leaving it to whatever OpenClaw's own default is.
OPENCLAW_MODEL_MIN_CONTEXT_WINDOW = 65536


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
        payload["models"] = [{"id": model, "name": model, "contextWindow": OPENCLAW_MODEL_MIN_CONTEXT_WINDOW}]
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
