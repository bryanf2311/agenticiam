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
OLLAMA_CLOUD_API_BASE = "https://ollama.com"
ANTHROPIC_API_BASE = "https://api.anthropic.com"
GOOGLE_API_BASE = "https://generativelanguage.googleapis.com"

# Ollama Cloud (hosted models on ollama.com, distinct from local Ollama)
# cannot be configured via env vars in Goose — confirmed against
# block/goose's providers/ollama_cloud.rs: its `from_env` implementation
# unconditionally bails with "Ollama Cloud must be configured as a
# declarative provider. Run `goose configure` to set it up." So unlike
# every other provider here, it needs a one-time interactive `goose
# configure` step before any generated launch command will work.
GOOSE_ENV_UNCONFIGURABLE_PROVIDERS = {"ollama_cloud"}

# Confirmed against block/goose's actual provider source (anthropic_def.rs,
# google_def.rs) and its provider docs table — not guessed. Note Gemini's
# env var is GOOGLE_API_KEY, not GEMINI_API_KEY, despite the product name.
PROVIDER_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def ollama_install_commands() -> dict:
    """Confirmed against ollama.com's own documented one-liner (macOS/Linux)
    and the winget package id `Ollama.Ollama` (Windows) — not guessed."""
    return {
        "bash": "curl -fsSL https://ollama.com/install.sh | sh && ollama --version",
        "powershell": "winget install -e --id Ollama.Ollama; ollama --version",
        "cmd": "winget install -e --id Ollama.Ollama && ollama --version",
    }


def goose_install_commands() -> dict:
    """Confirmed against block/goose's own installation docs — the CLI
    download script (not the Desktop app installer), with CONFIGURE=false
    so the script doesn't stop to interactively prompt for a provider."""
    return {
        "bash": "curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh | CONFIGURE=false bash",
        "powershell": (
            'Invoke-WebRequest -Uri "https://raw.githubusercontent.com/aaif-goose/goose/main/download_cli.ps1" '
            '-OutFile "download_cli.ps1"; .\\download_cli.ps1'
        ),
        "cmd": (
            "REM Goose's Windows installer is a PowerShell script; run this from PowerShell, not cmd.exe:\n"
            'REM Invoke-WebRequest -Uri "https://raw.githubusercontent.com/aaif-goose/goose/main/download_cli.ps1" '
            '-OutFile "download_cli.ps1"; .\\download_cli.ps1'
        ),
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


def list_ollama_cloud_models(api_key: str, timeout: float = 5.0) -> list:
    """Ollama Cloud (hosted models on ollama.com, distinct from local
    Ollama) uses the same /api/tags shape as local Ollama, just at
    ollama.com with Bearer auth instead of no-auth localhost — confirmed
    against block/goose's own ollama_cloud provider source
    (build_ollama_api_client uses AuthMethod::BearerToken against the
    configured host, and fetch_ollama_model_names hits "api/tags")."""
    if not api_key:
        raise ProviderUnavailable("an Ollama Cloud API key is required to list models")
    req = urllib.request.Request(
        f"{OLLAMA_CLOUD_API_BASE}/api/tags",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderUnavailable(str(exc)) from exc
    return sorted(m["name"] for m in data.get("models", []) if m.get("name"))


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
    if provider == "ollama_cloud":
        return list_ollama_cloud_models(api_key, timeout=timeout)
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


def secrets_path() -> Path:
    """Goose's file-backed secret store — used only when its OS keyring is
    disabled (env var GOOSE_DISABLE_KEYRING, confirmed against
    crates/goose/src/config/base.rs: `secret_storage()` falls back to
    `SecretStorage::File { path: config_dir.join("secrets.yaml") }`, a flat
    YAML key/value map, the same shape as config.yaml). Lives next to
    config.yaml, same directory."""
    return config_path().parent / "secrets.yaml"


def load_secrets() -> dict:
    path = secrets_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def save_secrets(secrets: dict) -> Path:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(f".yaml.bak-{int(time.time())}")
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(secrets, f, default_flow_style=False, sort_keys=False)
    return path


# The exact secret name Goose looks up for Ollama Cloud — confirmed
# against Goose's own bundled fixed-provider definition
# (crates/goose-providers/src/declarative/definitions/ollama_cloud.json:
# "api_key_env": "OLLAMA_CLOUD_API_KEY"). Ollama Cloud is a *fixed*
# (compile-time bundled) declarative provider, not something that needs a
# custom_providers/*.json file written — the only missing piece for
# `--provider ollama_cloud` to work standalone is this secret being
# resolvable.
OLLAMA_CLOUD_API_KEY_ENV = "OLLAMA_CLOUD_API_KEY"


def write_ollama_cloud_secret(api_key: str) -> Path:
    """Opt-in only (see launch_commands' auto_configure param) — writes the
    key in *plaintext* to secrets.yaml on disk. This is a real, deliberate
    departure from every other provider in this module, where the key is
    never persisted anywhere and only ever appears transiently in a
    one-time launch command. It exists because Goose's own ollama_cloud
    provider code refuses env-var configuration entirely (see
    GOOSE_ENV_UNCONFIGURABLE_PROVIDERS) — there is no transient option for
    this one provider, only "store it via the interactive `goose configure`
    keyring flow" or "store it in this file". Callers must surface that
    tradeoff, not silently opt into it."""
    secrets = load_secrets()
    secrets[OLLAMA_CLOUD_API_KEY_ENV] = api_key
    return save_secrets(secrets)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "agent"


def recipes_dir() -> Path:
    return config_path().parent / "agenticiam-recipes"


# The body of docs/manager-system-prompt.md (everything from "## Your Role"
# on) — embedded here rather than read from disk so it ships correctly
# inside the frozen PyInstaller exe with no extra bundling config. Keep
# this in sync with that file if either changes.
MANAGER_SYSTEM_PROMPT = """## Your Role

You are a manager agent. You can delegate tasks to other agents through the
**`iamDispatchToAgent`** MCP tool and get their text response back inline,
like a function call. You coordinate and synthesize — you do not do the
delegated work yourself.

## Before You Dispatch Anything

**Dispatch only works against Ollama-backed agents.** AgenticIAM never
stores Anthropic/Google API keys (by design — it's not a place third-party
provider credentials should live), so there is nothing to reconstruct a
dispatch call with for a cloud-backed agent. If you try to dispatch to one,
you'll get a clear error, not a hang.

Check who's actually dispatchable before you try, in the same call where
you check your own permissions:

```typescript
async function run() {
  const me = await Boss.iamWhoami();
  const myDispatchPerms = me.scopes.filter(s => s.startsWith('dispatch:'));

  const agents = await Boss.iamListIdentities({ kind: "agent" });
  const dispatchable = agents.filter(a =>
    a.name !== me.name &&
    a.metadata && a.metadata.goose && a.metadata.goose.provider === "ollama"
  );

  return {
    me: me.name,
    dispatchPermissions: myDispatchPerms,
    dispatchableWorkers: dispatchable.map(a => ({ name: a.name, model: a.metadata.goose.model })),
  };
}
```

An agent with no `metadata.goose` at all wasn't created through the New
Agent wizard and isn't dispatchable either, regardless of provider.

## How to Dispatch

Call it from **your own** extension namespace — not the target's. If you
are "Boss", it's `Boss.iamDispatchToAgent`, never
`TestManager.iamDispatchToAgent` or similar; you don't have another
agent's identity or its permissions.

```typescript
async function run() {
  const result = await Boss.iamDispatchToAgent({
    agent: "test-manager",       // exact identity name, case-sensitive
    task: "Clear, complete instructions — the worker has no other context.",
    timeout_seconds: 300,        // optional, default 300s
  });

  // result is { agent: "test-manager", response: "<the worker's text output>" }
  // THE TEXT YOU WANT IS result.response, NOT result ITSELF.
  return result;
}
```

**The single most important detail**: the return value is an object shaped
`{ agent: string, response: string }`. The worker's actual output is in
`.response`. If you pass the raw result object into a template string or a
follow-up task, you'll get `[object Object]`, not the text — always use
`.response`.

One more thing worth defending against: depending on how the runtime
surfaces MCP tool results to this sandbox, you may occasionally get back a
JSON *string* instead of an already-parsed object. A robust pattern:

```typescript
function unwrap(dispatchResult) {
  const parsed = typeof dispatchResult === 'string' ? JSON.parse(dispatchResult) : dispatchResult;
  return parsed.response;
}
```

## Never Use the Generic `delegate` Tool for This

Goose has its own built-in `delegate` tool for spawning ad-hoc subagents.
It is not connected to AgenticIAM at all — no permission check, no audit
trail, no routing to the actual `test-manager`/`test-intern` identities
you set up. If your instructions mention "delegate," that is a description
of what you're doing, not the name of a tool to call. Use
`iamDispatchToAgent` specifically.

## Sequential Dispatch (task B needs task A's result)

```typescript
async function run() {
  const managerResult = await Boss.iamDispatchToAgent({
    agent: "test-manager",
    task: "Search for recent IT news and identify the 3 most important items.",
  });

  const internResult = await Boss.iamDispatchToAgent({
    agent: "test-intern",
    task: `Write a summary file based on these findings:\\n\\n${managerResult.response}`,
  });

  return { managerFindings: managerResult.response, internReport: internResult.response };
}
```

## Parallel Dispatch (independent tasks)

```typescript
async function run() {
  const [a, b] = await Promise.all([
    Boss.iamDispatchToAgent({ agent: "worker-1", task: "Independent task 1" }),
    Boss.iamDispatchToAgent({ agent: "worker-2", task: "Independent task 2" }),
  ]);
  return { worker1: a.response, worker2: b.response };
}
```

## Troubleshooting

**`principal '<you>' lacks permission 'dispatch:<agent>'`** — you weren't
granted dispatch rights to that specific agent. Check
`(await Boss.iamWhoami()).scopes` for `dispatch:<name>` or `dispatch:*`;
if missing, an admin needs to grant it via the AgenticIAM web console
(Roles tab, or the wizard's "Manager permissions" step at creation time).

**`'<agent>' was not created as a Goose agent (no provider/model
metadata)`** — that identity exists in AgenticIAM but wasn't created
through the New Agent wizard (or was created before that metadata
existed). It can't be dispatched to; recreate it through the wizard.

**`dispatch currently only supports Ollama-backed workers`** — the target
is Anthropic/Google-backed. Dispatch cannot reach it (see "Before You
Dispatch Anything" above). Use an Ollama-backed worker instead, or run
that specific task yourself if it genuinely needs the bigger model.

**`dispatched task timed out after <N>s`** (may include partial
stdout/stderr) — the worker didn't finish in time. Default is 300s. Retry
with a higher `timeout_seconds`, or break the task into smaller pieces.
Partial output in the error, if present, tells you whether it was
genuinely still working or stuck on something specific.

**Agent name not found** — names are exact and case-sensitive. Re-check
`await Boss.iamListIdentities({ kind: "agent" })` rather than guessing.

## Complete Example

```typescript
async function run() {
  const me = await Boss.iamWhoami();
  const agents = await Boss.iamListIdentities({ kind: "agent" });
  const workers = agents.filter(a =>
    a.name !== me.name && a.metadata?.goose?.provider === "ollama"
  );
  console.log(`Manager: ${me.name}. Dispatchable workers: ${workers.map(w => w.name).join(', ')}`);

  const research = await Boss.iamDispatchToAgent({
    agent: "research-analyst",
    task: "Search for recent AI developments and summarize the 3 most important.",
    timeout_seconds: 600,
  });

  const report = await Boss.iamDispatchToAgent({
    agent: "report-writer",
    task: `Write a short report based on this research:\\n\\n${research.response}`,
    timeout_seconds: 300,
  });

  return { status: "complete", research: research.response, report: report.response };
}
```
"""


def manager_recipe_yaml(name: str, provider: str = None, model: str = None) -> str:
    """A Goose recipe (see block/goose's recipe-reference docs) whose
    `instructions` field is the manager system prompt above — loaded via
    `goose run --recipe ...` so a manager agent gets it automatically
    instead of requiring it to be pasted in by hand every session.

    Without an explicit `settings` block, a recipe silently falls back to
    Goose's own default provider/model (whatever was configured first via
    `goose configure`) instead of the one this manager was actually set up
    with — confirmed from a real report where a recipe launched with a
    completely different model than the agent was created with. The recipe
    reference docs describe `settings.goose_provider`/`goose_model` as
    exactly the override for this: "This overrides the default
    configuration when the recipe is executed."
    """
    recipe = {
        "version": "1.0.0",
        "title": f"{name} (manager)",
        "description": f"AgenticIAM manager recipe for '{name}' — preloads the dispatch-to-agent system prompt.",
        "instructions": MANAGER_SYSTEM_PROMPT,
    }
    if provider and model:
        settings = {"goose_provider": provider, "goose_model": model}
        recipe["settings"] = settings
    return yaml.safe_dump(recipe, default_flow_style=False, sort_keys=False)


def write_manager_recipe(name: str, provider: str = None, model: str = None) -> Path:
    directory = recipes_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slugify(name)}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        f.write(manager_recipe_yaml(name, provider=provider, model=model))
    return path


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


def remove_extension(config: dict, extension_id: str) -> dict:
    """Inverse of register_extension — used when deleting an identity that
    was registered as a Goose extension, so config.yaml doesn't accumulate
    dead entries pointing at a token that no longer authenticates."""
    config = dict(config)
    extensions = dict(config.get("extensions") or {})
    extensions.pop(extension_id, None)
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
    name: str,
    provider: str = None,
    model: str = None,
    context_limit: int = None,
    api_key: str = None,
    recipe_path=None,
    auto_configure: bool = False,
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

    `recipe_path`, if given, launches via `goose run --recipe ... --interactive`
    instead of `goose session` — confirmed against block/goose's recipe
    reference docs that `--recipe`/`--interactive` are `goose run`-only
    flags (`goose session` has no `--recipe` option). Used for manager
    agents so their dispatch system prompt (see write_manager_recipe) loads
    automatically instead of needing to be pasted in every session.

    A provider in GOOSE_ENV_UNCONFIGURABLE_PROVIDERS (currently just
    `ollama_cloud`) can't be selected via env vars at all, so this instead
    generates `goose run --provider <p> --model <m> -t 'Hello' --interactive`,
    which selects an *already-configured* provider — the caller is
    responsible for surfacing that the one-time `goose configure` setup is a
    prerequisite (see the Setup tab / README), unless `auto_configure` is
    set (see write_ollama_cloud_secret) — in which case this adds
    GOOSE_DISABLE_KEYRING=1 to the one-time command, scoped to just this
    invocation, so the file-backed secret AgenticIAM wrote is actually what
    Goose reads instead of trying (and missing) the OS keyring. It does not
    touch config.yaml, so it has no effect on any other provider's
    keyring-stored secrets in other Goose sessions.
    """
    env_unconfigurable = provider in GOOSE_ENV_UNCONFIGURABLE_PROVIDERS
    if recipe_path:
        base = f"goose run --recipe '{recipe_path}' --interactive -n {name}"
        ps_base = f"goose run --recipe '{recipe_path}' --interactive -n {name}"
        cmd_base = f'goose run --recipe "{recipe_path}" --interactive -n {name}'
    elif env_unconfigurable and model:
        # Can't select this provider via GOOSE_PROVIDER env var (see
        # GOOSE_ENV_UNCONFIGURABLE_PROVIDERS) — `goose run --provider/--model`
        # flags select an *already-configured* provider instead, which is
        # the only way to reach it once the one-time `goose configure` setup
        # (see the Setup tab) has been done.
        #
        # `goose run` also always requires -i/-t/--recipe — `--interactive`
        # only means "stay in the chat after the first turn," it does not
        # waive that requirement (confirmed against a real `goose run`
        # invocation, which rejected `--provider ... --model ... --interactive`
        # alone with "Must provide either --instructions (-i), --text (-t),
        # or --recipe"). So a starting -t text is required here too.
        base = f"goose run --provider {provider} --model '{model}' -t 'Hello' --interactive -n {name}"
        ps_base = f"goose run --provider {provider} --model '{model}' -t 'Hello' --interactive -n {name}"
        cmd_base = f'goose run --provider {provider} --model "{model}" -t "Hello" --interactive -n {name}'
    else:
        base = ps_base = cmd_base = f"goose session -n {name}"
    bash_parts, ps_parts, cmd_parts = [], [], []

    def add(key, value, quote=False):
        if quote:
            bash_parts.append(f"{key}='{value}'")
            ps_parts.append(f"$env:{key}='{value}'")
        else:
            bash_parts.append(f"{key}={value}")
            ps_parts.append(f'$env:{key}="{value}"')
        cmd_parts.append(f'set "{key}={value}"')

    if env_unconfigurable and auto_configure:
        add("GOOSE_DISABLE_KEYRING", "1")
    if model and not env_unconfigurable:
        provider = provider or "ollama"
        add("GOOSE_PROVIDER", provider)
        add("GOOSE_MODEL", model)
    if context_limit:
        add("GOOSE_CONTEXT_LIMIT", context_limit)
        if (provider or "ollama") == "ollama":
            add("GOOSE_INPUT_LIMIT", context_limit)
    key_env = PROVIDER_API_KEY_ENV.get(provider)
    if key_env and api_key and not env_unconfigurable:
        add(key_env, api_key, quote=True)

    if not bash_parts:
        return {"bash": base, "powershell": ps_base, "cmd": cmd_base}
    return {
        "bash": " ".join(bash_parts) + f" {base}",
        "powershell": "; ".join(ps_parts) + f"; {ps_base}",
        "cmd": " && ".join(cmd_parts) + f" && {cmd_base}",
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
    popen_kwargs = {}
    if os.name == "nt":
        # Without this, Windows pops up a new, empty console window for the
        # child process — the parent (agenticiam-gui.exe, or any MCP stdio
        # server with no console of its own) has no console to inherit into,
        # so Windows allocates one, even though stdout/stderr are already
        # being captured via pipes. Dispatch is meant to run invisibly in
        # the background; the window was never intentional.
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **popen_kwargs)
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
