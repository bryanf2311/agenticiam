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
`iam_issue_api_key`, and `iam_audit_log` — enough to fully administer the
directory from inside a Claude session if the token's identity has
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

## CLI reference

```
agenticiam init                          bootstrap the directory + admin identity
agenticiam serve [--host] [--port]       run the REST/OAuth2 server (also serves the web console at /)
agenticiam gui [--host] [--port] [--no-browser]   serve + auto-open the web console in your browser
agenticiam mcp                           run the MCP stdio server (reads AGENTICIAM_TOKEN)
agenticiam whoami --token TOKEN          resolve a token to an identity + scopes

agenticiam user add|list                 human identities
agenticiam agent add|list|rotate-secret  AI agent / service identities
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
# -> dist/agenticiam            CLI (console app)                (Linux/macOS)
# -> dist/agenticiam.exe        CLI (console app)                (Windows)
# -> dist/agenticiam-gui        GUI (opens a browser, no console) (Linux/macOS)
# -> dist/agenticiam-gui.exe    GUI (opens a browser, no console) (Windows)
```

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
