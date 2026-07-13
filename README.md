# AgenticIAM

A self-hosted identity, directory, and permission gateway for AI agents —
built to feel like running a small Active Directory domain, except the
"computers" joining the domain are AI agents and agent gateways instead of
Windows PCs.

One process. One SQLite file. No external dependencies to stand up. Point
your agents at it and manage who-can-do-what from a single directory.

## Why

Agent frameworks (Claude Desktop, Claude Code, Claude Cowork, OpenClaw,
and others) are increasingly given real privileges — running shell
commands, touching your filesystem, controlling a browser, sending email.
Each framework tends to invent its own ad-hoc auth. AgenticIAM gives you
one place to:

- Register every human, AI agent, and service as an **identity**
- Organize them into **groups** (like AD security groups)
- Define **roles** that bundle **permissions** (`shell:exec`, `files:*`,
  `browser:control`, `email:send`, `iam:admin`, ...) and assign those
  roles to identities or groups
- Issue **OAuth2 tokens** or long-lived **API keys** scoped to exactly
  what an agent is allowed to do
- Let any agent or gateway ask **"is this allowed?"** before it acts, and
  get every decision written to an **audit log**

## Architecture

```
                     ┌─────────────────────────┐
                     │   agenticiam (one exe)   │
                     │                          │
  agenticiam CLI ───▶│  directory.py (SQLite)   │◀─── agenticiam mcp (stdio)
  (local admin,      │  policy.py   (RBAC)      │     Claude Desktop / Code /
  like running       │  tokens.py   (HS256 JWT) │     Cowork MCP config
  tools on a DC)     │                          │
                     │  api.py (Flask, HTTP):   │◀─── OpenClaw gateway, or any
  agenticiam-gui ───▶│   /oauth/token           │     HTTP-capable agent:
  (double-click,     │   /oauth/introspect      │       1. get a token
  opens browser to   │   /v1/authorize  (PDP)   │       2. POST /v1/authorize
  the same server)   │   /v1/admin/*            │          before every
                     │   /  (web admin console) │          privileged action
                     └─────────────────────────┘
```

- **Directory**: identities are `user` (a human), `agent` (an AI agent),
  or `service`. Agents/services authenticate like OAuth2 clients
  (client_id + client_secret). No organizational-unit tree — groups cover
  the same ground with less ceremony.
- **RBAC**: permission strings are namespaced (`files:read`,
  `shell:exec`, `iam:admin`) and support wildcards (`files:*`, `*`).
  Roles carry permissions; roles are assigned to identities or groups;
  effective permissions are the union across both.
- **Tokens**: HS256 JWTs signed with a per-install symmetric key
  (generated on first run), the same "one domain, one shared secret"
  trust model Kerberos uses inside an AD domain. Short-lived (1h
  default) and **not individually revocable** — see Limitations. For
  revocable, long-lived credentials, issue an API key instead.
- **MCP server**: a hand-rolled (no SDK dependency) MCP stdio server, so
  Claude Desktop / Claude Code / Claude Cowork can manage and query the
  directory as a set of tools directly in an agent session.
- **REST/OAuth2 API**: `client_credentials` grant (RFC 6749 §4.4),
  RFC 7662-shaped introspection, and a `/v1/authorize` policy-decision
  endpoint — the integration point for gateways like **OpenClaw** that
  need to check permission before running a shell command, touching a
  file, or sending an email on an agent's behalf.

## Quickstart

### Easiest: the GUI

Download or build `agenticiam-gui` (see "Building the exe"), double-click
it. It starts the directory server and opens your browser to a first-run
setup screen — pick an admin username/password and you're in. From there
the web console covers everything: creating agent/service/user identities,
groups, roles and permissions, issuing revocable API keys, generating a
ready-to-paste MCP config, and browsing the audit log. No terminal
required.

### Or: the CLI

```bash
# Build (or download from a GitHub Actions run — see "Building the exe")
pip install -e ".[dev]"
pyinstaller agenticiam.spec
./dist/agenticiam init
#   -> prints a one-time admin secret, save it

# Create an AI agent identity (this is what OpenClaw / any agent process authenticates as)
./dist/agenticiam agent add openclaw-gateway
#   -> prints client_id + client_secret, save them

# Define what it's allowed to do
./dist/agenticiam role add gateway-ops
./dist/agenticiam role grant gateway-ops "shell:exec"
./dist/agenticiam role grant gateway-ops "files:*"
./dist/agenticiam role assign gateway-ops identity openclaw-gateway

# Run the directory server (defaults to loopback only)
./dist/agenticiam serve --host 127.0.0.1 --port 8765
```

Get a token and check a permission:

```bash
curl -u openclaw-gateway:<client_secret> \
     -d grant_type=client_credentials \
     http://127.0.0.1:8765/oauth/token
# => {"access_token": "...", "scope": "shell:exec files:*", ...}

curl -X POST http://127.0.0.1:8765/v1/authorize \
     -H "Content-Type: application/json" \
     -d '{"token": "<access_token>", "action": "shell:exec"}'
# => {"allow": true, "subject": "openclaw-gateway", "action": "shell:exec"}
```

## Setup tab (install Ollama, Goose, OpenClaw + hardware-based model picks)

The **Setup** tab in the web console (first item in the nav) is a
one-stop page for getting the tools the New Agent wizard drives actually
installed on the machine running `agenticiam serve`/`gui`:

- Copy-ready install commands (bash/PowerShell) for **Ollama**, **Goose**,
  and **OpenClaw** — pulled from each project's own documented install
  method (Ollama's `install.sh`/winget package, Goose's CLI download
  script, OpenClaw's `install.sh`/`install.ps1` + `openclaw onboard`), not
  guessed.
- A small hardware form (system RAM, optional GPU VRAM) that sorts a
  curated list of popular Ollama models into two tiers: **Fast** (the
  whole model fits in VRAM with headroom for context — fully
  GPU-accelerated) and **Usable** (too big for VRAM alone but fits once
  spilling into system RAM is allowed — works, just slower). Each
  recommendation comes with its `ollama pull <model>` command. Sizes are
  approximate (Q4_K_M, Ollama's common default quantization) — a starting
  point, not an exact fit, and this is a static curated table (there's no
  reliable API for "how much VRAM does model X need"), not a live catalog.

Example: 64GB RAM + an 8GB GPU puts every 7-9B model (Llama 3.1 8B, Qwen
2.5 7B/Coder 7B, Mistral 7B, Gemma 2 9B) in the Fast tier, and 14B-32B
models plus even a 70B in Usable — GPU-offloaded where it fits, spilling
into your 64GB of system RAM for the rest, meaningfully slower but not
unusable for non-realtime tasks.

The Setup tab also has an **Ollama Cloud** panel — hosted, bigger models
via an API key from `ollama.com/settings/keys`, nothing to install since
it's not a local server. See "One thing worth knowing about Ollama
Cloud" below the wizard section for the Goose-vs-OpenClaw asymmetry (one
needs a manual one-time `goose configure` step, the other doesn't).

## Web admin console

`agenticiam serve` (and `agenticiam gui`, which is just `serve` plus
auto-opening a browser) serves a full admin UI at `/` — no separate
install, it's baked into the same executable and process. First run shows
a one-time setup screen to create the admin account; after that it's a
normal login. Everything in the CLI's `identity`/`group`/`role`/`api-key`/
`audit` commands has a page: identities, group membership, role
permissions and assignments, API key issuance/revocation, an MCP config
generator (picks an agent identity, mints an API key, and gives you a
paste-ready `mcpServers` JSON block), and the audit log.

It's a thin client over the same `/v1/admin/*` REST API described below —
anything you can do in the browser you can also script against those
endpoints directly.

## New Agent wizard (Goose or OpenClaw + Ollama/Ollama Cloud/Anthropic/Google)

The **+ New Agent** button in the web console is a 4-step wizard that goes
from nothing to a running, permissioned AI agent:

1. **Target & prerequisites** — pick which runtime this agent lives in:
   - **Goose** — a single interactive local session per agent
     (`goose session` / `goose run`). Checks whether
     [Goose](https://block.github.io/goose/) is on `PATH`.
   - **OpenClaw** — a persistent gateway persona reachable from chat apps
     (Discord, Telegram, WhatsApp, ...) via [OpenClaw](https://openclaw.ai/).
     Checks whether `openclaw` is on `PATH`.

   Ollama is checked too but only needed if you pick it as a provider in
   the next step. Not installed? See the Setup tab above for install
   commands for all three.
2. **Name, provider & model** — pick a name, then a provider:
   - **Ollama** (local, free) — models are listed live from Ollama's local API.
   - **Ollama Cloud** (hosted, bigger models) — Ollama's own hosted API
     (distinct from local Ollama; get an account + API key at
     `ollama.com/settings/keys`). Paste the key and click "Load models" to
     fetch the live list from `ollama.com/api/tags`, same shape as local
     Ollama's `/api/tags`, just Bearer-authenticated. See "One thing worth
     knowing about Ollama Cloud" below — it needs one manual step for a
     Goose-target agent, none for OpenClaw.
   - **Anthropic** (Claude) or **Google** (Gemini) — paste an API key and
     click "Load models" to fetch the live list from that provider's own
     API (`api.anthropic.com/v1/models` / `generativelanguage.googleapis.com`).
     **The key is never stored by AgenticIAM** — not in its database, not
     in Goose's `config.yaml`. It only ever appears once, embedded in the
     launch command shown in the final step, matching Goose's own guidance
     against keeping provider API keys in plaintext config files (it
     expects them via env var or its OS-keyring-backed secret store). This
     applies to the Ollama Cloud key too.
3. **Permissions** — check off common scopes (`shell:exec`, `files:read`,
   `files:write`, `browser:control`, `email:send`) or type custom ones;
   this becomes a role scoped to just this agent. There's also an
   optional **Group** field here — put the agent in a team (e.g.
   "marketing") alongside its teammates; see "Teams" below.
4. **Review & create** — set an optional context window (tokens), then on
   confirm it creates the agent identity, role, and permission grants
   (same directory primitives as everything else in this doc), mints an
   API key, and registers an `extensions` entry in Goose's `config.yaml`
   with *that* key (AgenticIAM's own, revocable, low-stakes one) embedded
   in `envs.AGENTICIAM_TOKEN` — never as a shell string, so it never shows
   up in a process listing. You get back a ready-to-paste `goose session
   -n <name>` command to start chatting, in bash/PowerShell/cmd variants
   — or, for a manager (see below), a `goose run --recipe ...
   --interactive` command instead, so its dispatch system prompt loads
   automatically.

   **For an OpenClaw-target agent**, step 4 looks the same but AgenticIAM
   never touches OpenClaw's own config file — it's JSON5 (comments,
   trailing commas) that Python's stdlib `json` can't round-trip safely,
   and OpenClaw ships a CLI built for exactly this. Instead you get two
   commands to run: `openclaw mcp add <name> --command ... --env
   AGENTICIAM_TOKEN=...` (registers AgenticIAM's tools, the OpenClaw
   equivalent of Goose's `config.yaml` extension) and `openclaw agents add
   <name> --model <provider>/<model> --non-interactive --workspace ...`
   (creates the persona). A manager agent gets a `SOUL.md` written into
   that workspace directory instead of a Goose recipe — same dispatch
   system prompt, OpenClaw's own mechanism for a persona's system prompt.

### One thing worth knowing about Ollama Cloud

Ollama Cloud behaves asymmetrically across the two targets, and it's not
an AgenticIAM limitation — it's how each runtime's own code handles it:

- **Goose**: confirmed against block/goose's `providers/ollama_cloud.rs`
  source, its `from_env` implementation unconditionally returns an error —
  *"Ollama Cloud must be configured as a declarative provider. Run `goose
  configure` to set it up."* Every other provider in this wizard can be
  selected purely through a one-time launch command's environment
  variables; Ollama Cloud can't, by Goose's own design. It's also
  confirmed to be a *fixed* (compile-time bundled) declarative provider —
  its definition ships inside the Goose binary itself
  (`crates/goose-providers/src/declarative/definitions/ollama_cloud.json`,
  `api_key_env: "OLLAMA_CLOUD_API_KEY"`) — so there's no need to hand-author
  a custom provider; the only missing piece is that API key being
  resolvable through Goose's own secret store. Two ways to give it that,
  both shown in the wizard:
  - **Manual (default)**: step 5 shows a one-time-setup callout — run
    `goose configure` once, choose **Ollama Cloud** from the provider list,
    paste the key there. Goose stores it in your OS keyring (Windows
    Credential Manager / Keychain / Secret Service) — the most secure
    option, but it's an interactive step you have to do yourself before
    the generated command works.
  - **Auto-configure (opt-in checkbox in step 2)**: skips that step
    entirely. AgenticIAM writes the key directly to Goose's
    `secrets.yaml` — confirmed from `crates/goose/src/config/base.rs`
    to be a plain flat YAML file Goose falls back to when its OS keyring
    is disabled via `GOOSE_DISABLE_KEYRING` — and the generated launch
    command sets that env var, scoped to just that one invocation (never
    written to config.yaml, so it has zero effect on any other provider's
    keyring-stored secrets in other Goose sessions). The real tradeoff,
    stated in the UI: **the key sits in plaintext on disk** instead of
    your OS credential store — a genuine, deliberate exception to this
    module's usual "never persist provider keys anywhere" rule, made only
    because Goose leaves no other automatable option for this one
    provider. Off by default; your call per agent.

  Either way, once configured, the generated `goose run --provider
  ollama_cloud --model <model> --interactive -n <name>` command works
  normally (`--provider`/`--model` select an already-configured provider,
  unlike env vars, which are the piece that's blocked).
- **OpenClaw**: fully scriptable, no manual step. OpenClaw's model
  provider config (`models.providers.<id>`) is plain JSON reachable
  through its own `config set --merge` CLI, not a keyring-backed
  declarative-provider file — so the wizard's step 5 includes an extra
  first command, `openclaw config set models.providers.ollama-cloud
  '{"baseUrl":"https://ollama.com/v1","apiKey":"...","api":"openai-completions"}'
  --strict-json --merge`, before the usual tool-registration and
  agent-creation commands.

Two things worth knowing:

- **Same machine only.** This talks to `goose`'s config file and Ollama's
  API on whatever machine AgenticIAM's server process is running on. If
  you're driving the web console from a browser on a *different* machine
  than the one running `agenticiam serve`/`gui`, the wizard is checking
  and writing to the server's machine, not yours.
- **Goose has no per-agent model profile, and `goose session` takes no
  `--provider`/`--model` flags** (only `goose run` does — verified
  against a real install, not just docs). `GOOSE_PROVIDER`/`GOOSE_MODEL`/
  `GOOSE_CONTEXT_LIMIT` (context window, tokens) are global settings in
  config.yaml, all readable from the environment too. So by default the
  wizard leaves your global settings alone and hands you a launch command
  that sets them as environment variables for just that one invocation —
  along with the provider's API key env var (`ANTHROPIC_API_KEY` /
  `GOOGLE_API_KEY`) when applicable, and `GOOSE_INPUT_LIMIT` alongside
  `GOOSE_CONTEXT_LIMIT` for Ollama specifically, since that's the setting
  that actually reaches Ollama's `num_ctx`. Checking "set as Goose's
  default" in step 4 writes provider/model/context into config.yaml
  instead — that affects every future `goose session`, not just this
  agent, and the launch command drops those env vars since they're no
  longer needed (the API key env var, if any, always still appears in the
  command — it's never written to config.yaml regardless).
- If writing `config.yaml` fails (permissions, read-only filesystem, ...)
  the agent identity/role/key are still created — you just get the raw
  YAML `extensions` snippet to paste in by hand instead of losing the
  work. The directory is the source of truth; the config write is
  best-effort.
- **Either shipped executable works as the extension's command.** The GUI
  exe isn't GUI-only: run it with any arguments (`agenticiam-gui mcp`,
  `agenticiam-gui init`, ...) and it behaves exactly like the CLI exe;
  with none, it opens the browser. So the wizard always points the
  extension at whichever binary is actually running the server, and it
  works whether you have one executable on disk or both.
- **Deleting a Goose-target agent cleans up after itself.** Deleting it
  also removes that `extensions` entry from `config.yaml` (with the usual
  backup-first write) so you don't accumulate dead entries pointing at a
  token that no longer authenticates. You get a confirmation screen either
  way — what was removed and from where, or, if the automatic cleanup
  fails (permissions, read-only FS), the exact block to delete by hand
  instead. The identity itself is always gone from the directory
  regardless of whether the config.yaml cleanup succeeds.
- **Deleting an OpenClaw-target agent** removes its `SOUL.md` (if it had
  one, i.e. it was a manager) but *not* the `openclaw mcp`/`openclaw
  agents` entries — those live in OpenClaw's own config, which AgenticIAM
  never writes to directly (see above). The delete confirmation gives you
  the exact `openclaw mcp unset <name>` command and points you at
  `openclaw agents list` to remove the persona if you no longer want its
  workspace/session history.

## Teams (groups)

Groups are AgenticIAM's existing security-group primitive (see the web
console's Groups tab) — the wizard's basic team workflow is: create
"marketing-boss", "marketing-manager", and "marketing-intern" through the
wizard, typing the same group name ("marketing") into each one's optional
Group field in step 3 (or add them to an existing group afterward from
the Groups tab).

Once they're grouped, open that group in the Groups tab and click **Show
start commands for this team** — it lists every Goose-linked member's
launch command (bash/PowerShell/cmd, same as the wizard's final screen)
in one place, so you can open a terminal tab per teammate and get the
whole team running. This is deliberately just a list of commands to
copy, not an automatic multi-window launcher — actually spawning several
interactive terminal sessions reliably from a background web server
process is OS-specific and not something to fake without being able to
verify it actually works; the commands are the reliable part.

## Multi-agent: managers dispatching to workers

Step 3 of the wizard has a "Manager permissions" section: check off which
existing agents this new one should be able to delegate tasks to, or
check "any agent" for `dispatch:*` (available even when creating the
very first agent — it isn't limited to agents that already exist). No
containers, no separate worker processes to manage — a "worker" is just
another agent created by the same wizard, and "manager" isn't a special
identity kind, it's just a role that's been granted `dispatch:<name>`
permissions. Any agent/role combination can be a manager of any other.

**Checking any manager permission automatically preloads the dispatch
system prompt**, so you don't paste it in by hand — the delivery
mechanism depends on target:

- **Goose**: creating the agent writes a
  [Goose recipe](https://block.github.io/goose/docs/guides/recipes/recipe-reference/)
  — a small YAML file whose `instructions` field is the full dispatch guide
  (how to call `iamDispatchToAgent` correctly, the `.response` field
  gotcha, which providers dispatch can reach, troubleshooting) — to
  `agenticiam-recipes/<name>.yaml` next to Goose's `config.yaml`, along
  with a `settings.goose_provider`/`goose_model` block so the recipe
  actually launches with the model this agent was created with instead of
  silently falling back to Goose's own global default (a real bug we hit:
  a recipe with no settings block ignored the configured provider/model
  entirely). The launch command the wizard hands you then uses `goose run
  --recipe <path> --interactive -n <name>` instead of plain `goose
  session -n <name>`, so opening that session starts the manager already
  knowing how to dispatch. The recipe file is deleted automatically when
  you delete the agent.
- **OpenClaw**: the same content is written as `SOUL.md` into the agent's
  workspace (`~/.openclaw/workspace-<name>/SOUL.md`) — OpenClaw's own
  mechanism for a persona's system prompt.

The same content, for reference or manual use elsewhere, is at
[`docs/manager-system-prompt.md`](docs/manager-system-prompt.md).

Once granted, the manager's MCP session gets a new tool,
`iam_dispatch_to_agent(agent, task)`: it runs the task through the named
worker's own provider/model via `goose run --no-session` (one-shot, not
an ongoing conversation) and returns the text response inline — so the
manager's model can call it mid-conversation like a function call and use
the result. **This always shells out to the `goose` binary, even for an
OpenClaw-target worker** — dispatch is orthogonal to which runtime the
worker's own interactive session uses, it just needs a provider+model
pair. So Goose needs to be installed for dispatch to work regardless of
which target you picked when creating the workers. The same capability is
available over REST at `POST /v1/agents/<name>/dispatch` (body
`{"task": "..."}`, any bearer token with the right `dispatch:` scope —
not admin-gated, so a manager agent's own API key works) for non-MCP
callers like OpenClaw, and from the terminal via `agenticiam agent
dispatch <name> "<task>"`.

**Dispatch works against Ollama and Ollama Cloud workers — not
Anthropic/Google.** AgenticIAM never stores an Anthropic/Google API key
(see the wizard section above), so there's nowhere to pull one back out
of when a dispatch call comes in later; those cleanly error instead of
silently failing or needing you to paste a key into every dispatch call.
Ollama and Ollama Cloud are different: Goose resolves those keys itself
(none needed at all for local Ollama; Ollama Cloud's key lives in Goose's
own keyring/secrets.yaml, set up via `goose configure` or the wizard's
auto-configure option — see "One thing worth knowing about Ollama
Cloud" — never by AgenticIAM). Dispatch's own subprocess call sets
`GOOSE_DISABLE_KEYRING=1` automatically (scoped to just that call) when
the target worker was set up via auto-configure, so it can find the key
in secrets.yaml the same way its own launch command would; a
manually-`goose configure`d worker needs nothing extra. In practice this
maps naturally onto the shape most people want anyway: cheap/fast local
Ollama models as workers, a bigger cloud model (typically Claude, via
Anthropic, or a bigger Ollama Cloud model — but nothing enforces that) as
the manager calling `dispatch` from its own session.

`goose run` (unlike `session`) genuinely accepts `--provider`/`--model`
flags — confirmed against the CLI docs and consistent with everything
else in this README that touches Goose flags, all of which is verified
against a real install, not assumed.

### Debugging a dispatch call

Every dispatch — from MCP, REST, or the CLI — now writes a `started`
audit entry (provider, model, timeout) *before* running anything, then a
`success` or `failure` entry with elapsed time when it finishes. Tail it
with `agenticiam audit tail -n 20` while a dispatch is in flight; if you
only ever see `started` with no matching `success`/`failure`, it's still
running (or the process was killed some other way) — `agenticiam` itself
didn't silently swallow it.

The default dispatch timeout is 300 seconds, matching the timeout
already configured for the extension itself in `config.yaml` (an earlier
version of this had them mismatched — dispatch would give up internally
at 120s while the extension was allowed 300s, meaning our own code was
cutting things off before Goose's own allowance would have). Pass a
longer one for a known-slow task: `timeout_seconds` in the MCP tool call
or the REST JSON body, `--timeout` on the CLI. If it still times out, the
error now includes whatever partial stdout/stderr the `goose run`
subprocess had already produced — that's usually enough to tell "it's
genuinely still generating" apart from "it's stuck on something."

One thing worth checking empirically if dispatch is consistently slow:
every extension marked `enabled: true` in `config.yaml` — which includes
every agent the wizard has ever created for you — may get loaded by
Goose for *every* `goose run`/`session` invocation, not just the one
being dispatched to. If you have several agents registered, a single
dispatch call could be spinning up several redundant `agenticiam(-gui)`
child processes in the background before the actual model call even
starts. Open Task Manager (or `ps` on Linux/macOS) right when you kick
off a dispatch and watch how many `agenticiam`/`agenticiam-gui`
processes appear — if it's more than one, that's very likely adding real
latency on top of local-model inference itself. This isn't something
AgenticIAM's code controls (it's how Goose loads extensions), so there's
no code fix for it here yet, but it's worth knowing about if 300s still
isn't enough on modest hardware with several registered agents.

Two more things fixed after real-world testing, both Windows-specific:

- **`agenticiam-gui.exe mcp` used to crash with `OSError: [Errno 22]
  Invalid argument`** if the parent Goose session was closed while it
  still had something to say (e.g. you closed the console mid-dispatch).
  Writing to a stdio pipe the other end already closed is a normal
  end-of-session condition, not a bug — it now exits silently (exit code
  0, no traceback) instead. This uses `os._exit()` rather than a plain
  `sys.exit()`: the latter still lets Python's normal shutdown sequence
  try to flush stdout one more time, which hits the *same* broken pipe
  and makes CPython override the exit code to its own hardcoded 120
  regardless of what was requested — `os._exit()` skips that shutdown
  sequence entirely.
- **A dispatch call used to pop up a visible, empty console window** on
  Windows. `agenticiam-gui.exe` has no console of its own (it's a
  windowed-subsystem app), so when it spawns `goose run` as a child
  process, Windows allocates one — independent of `stdout`/`stderr`
  already being captured via pipes for the dispatch response. Dispatch
  now passes `CREATE_NO_WINDOW` on Windows so it runs fully in the
  background, matching what "the manager gets the text response back"
  was always supposed to feel like.

## Integrating with Claude Desktop / Claude Code / Claude Cowork (MCP)

Issue a token for whichever identity the session should act as, then point
an MCP client config at the binary:

```bash
./dist/agenticiam token issue openclaw-gateway --ttl 43200   # 12h
```

```json
{
  "mcpServers": {
    "agenticiam": {
      "command": "/path/to/dist/agenticiam",
      "args": ["mcp"],
      "env": { "AGENTICIAM_TOKEN": "<token from above>" }
    }
  }
}
```

The agent now has tools like `iam_whoami`, `iam_check_permission`,
`iam_create_identity`, `iam_create_group`, `iam_assign_role`,
`iam_issue_api_key`, `iam_dispatch_to_agent` (see "Multi-agent" above),
and `iam_audit_log` — enough to fully administer the directory and
delegate to other agents from inside a Claude session if the token's
identity has
`iam:admin`, or just to introspect its own permissions if not.

## Integrating with OpenClaw

OpenClaw's gateway runs privileged actions (shell, files, browser, email)
triggered from chat messages. Give the gateway process its own
`agent` identity and a role scoped to only what it should be allowed to
do, then have it call the directory before each privileged action:

1. On startup, the gateway does the `client_credentials` grant above to
   get a token (re-fetch when it's near `expires_in`, or use a
   long-lived `api-key issue` instead — see below).
2. Before running a shell command / touching a file / sending an email,
   POST to `/v1/authorize` with `{"token": ..., "action": "shell:exec"}`
   and respect the `allow` field.
3. Every decision is written to the audit log
   (`agenticiam audit tail`), so you have a record of what every
   channel-triggered agent action was allowed or denied to do.

For a gateway process that shouldn't have to re-authenticate hourly, use
a revocable API key instead of an OAuth token:

```bash
./dist/agenticiam api-key issue openclaw-gateway --scope shell:exec --scope files:read
# use the returned key as a Bearer token against /v1/authorize and /v1/whoami;
# revoke it any time with: agenticiam api-key revoke <key-id>
```

## OpenClaw Agents tab: live agent list & permissions

Unlike the rest of OpenClaw integration (which generates commands for you
to run), this tab shells out to the local `openclaw` CLI directly from
AgenticIAM's own server — the same way dispatch already shells out to
`goose` — so it needs `openclaw` installed on whichever machine is running
`agenticiam serve`/`gui`.

It shows every agent OpenClaw currently knows about (`openclaw agents
list --json`), not just ones created through the wizard, and lets you
edit each one's real permissions in place:

- **Tool permissions** — per agent, per tool (`read`, `write`, `edit`,
  `process`, `bash`, `browser`, `cron`, `discord`, `gateway`, `canvas`,
  `nodes`, the `sessions_*` tools): Default (inherits the global policy),
  Allow, or Deny. Writes to `agents.list[<idx>].tools.allow`/`.deny`.
- **Filesystem access** — a list of `host:container:ro`/`:rw` bind
  mounts (`agents.list[<idx>].sandbox.docker.binds`), OpenClaw's actual
  per-path access control. This — and the network setting below — only
  takes effect while the agent is sandboxed (mode isn't "off"); outside a
  sandbox, an agent with `read`/`write` allowed has ordinary host
  filesystem access and there's no per-path allowlist to layer on top.
- **Sandbox settings** — mode (`off`/`non-main`/`all`), workspace access
  (`none`/`ro`/`rw`), and network (default, or `none` to cut a sandboxed
  agent off entirely).
- **Website access** — OpenClaw has no per-agent domain allowlist, only a
  per-agent on/off toggle for the `browser` tool (above) plus one
  **global** hostname allowlist (`browser.ssrfPolicy.hostnameAllowlist`)
  shared by every agent with browser access enabled. The tab surfaces
  both, with the global list clearly labeled as affecting every agent,
  not just the one you're editing.

If `openclaw` isn't installed, or the gateway isn't reachable, the tab
says so and points at the Setup tab instead of failing silently.

One quirk worth knowing about: on at least one real openclaw build, the
CLI process doesn't reliably exit on its own once it's done its work
(seen as `openclaw agents list --json` "timing out" even though it had
already printed a complete, correct JSON array well within the timeout —
something's keeping its Node process alive after the actual command is
finished). AgenticIAM works around this rather than surfacing a false
failure: reads recover a complete result straight out of the timeout if
one was produced; writes that time out are verified with a follow-up
read before being reported as failed.

## CLI reference

```
agenticiam init                          bootstrap the directory + admin identity
agenticiam serve [--host] [--port]       run the REST/OAuth2 server (also serves the web console at /)
agenticiam gui [--host] [--port] [--no-browser]   serve + auto-open the web console in your browser
agenticiam mcp                           run the MCP stdio server (reads AGENTICIAM_TOKEN)
agenticiam whoami --token TOKEN          resolve a token to an identity + scopes

agenticiam user add|list                 human identities
agenticiam agent add|list|rotate-secret  AI agent / service identities
agenticiam agent dispatch NAME TASK      run a task through a Goose-linked (Ollama/Ollama Cloud) agent
agenticiam identity show|enable|disable|rm|permissions NAME

agenticiam group add|list|add-member|remove-member|members
agenticiam role add|list|permissions|grant|revoke|assign|unassign

agenticiam token issue|introspect
agenticiam api-key issue|revoke|list
agenticiam audit tail [-n LIMIT]
```

## Building the exe

```bash
./scripts/build.sh
# -> dist/agenticiam            console app; run with no args for --help  (Linux/macOS)
# -> dist/agenticiam.exe        console app; run with no args for --help  (Windows)
# -> dist/agenticiam-gui        no console; run with no args to open the browser,
# -> dist/agenticiam-gui.exe    or with any argument (e.g. `mcp`) to behave like the CLI
```

You only need one of the two — `agenticiam-gui` handles both the double-click
case and the CLI case (`agenticiam-gui mcp`, `agenticiam-gui init`, ...).
`agenticiam` (console, no browser-opening) exists for scripting/CI contexts
where you never want it trying to launch a browser.

One `pyinstaller agenticiam.spec` invocation builds both executables.
PyInstaller doesn't cross-compile — build on the OS you're targeting.
`.github/workflows/build.yml` builds all three OSes (Linux/Windows/macOS)
on every push and uploads all four binaries as workflow artifacts, if
you'd rather not build locally on Windows/macOS.

## Limitations (v1)

This intentionally does **not** try to be full Active Directory —
multi-master replication, Kerberos delegation, GPOs, and cross-domain
trusts are all out of scope. Specifically:

- **No OAuth authorization-code flow / SSO federation.** Human login is a
  plain username+password (`/v1/login`) used by the web console, not a
  redirect-based OAuth/OIDC flow other identity providers could federate
  with. Fine for direct use; not a drop-in SSO participant.
- **First-run setup has no invite/multi-admin flow.** `/v1/setup/bootstrap`
  creates exactly one initial admin (whoever gets there first); add more
  admins afterward via the console or CLI.
- **JWT access tokens aren't individually revocable** before they
  expire (default 1h TTL) — the signing key is only rotatable
  wholesale. Use API keys (`agenticiam api-key issue`) for credentials
  you need to revoke on demand.
- **Single symmetric signing key per install** — every resource server
  that wants to verify a JWT itself (rather than calling
  `/oauth/introspect`) needs that same key; there's no per-domain
  JWKS/PKI federation yet.
- **Single-instance, single-file SQLite** — this is one domain
  controller, not a replicated cluster. Fine for a self-hosted/LAN
  deployment; back up `directory.db` like you would an AD database.
- `agenticiam serve` binds to `127.0.0.1` by default. If you expose it
  beyond loopback, put it behind TLS (a reverse proxy is the easiest
  path) — the built-in Flask server has no TLS of its own.

## License

MIT
