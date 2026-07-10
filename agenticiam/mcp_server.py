"""A minimal, dependency-free MCP server (JSON-RPC 2.0 over newline-delimited
stdio) exposing the directory as tools. This is the integration point for
Claude Desktop, Claude Code, and Claude Cowork: point an MCP client config
at `agenticiam mcp` with AGENTICIAM_TOKEN set in its env, and the agent can
manage and query the directory as that identity for the rest of the session.

No MCP SDK dependency on purpose — the stdio transport is simple enough
(initialize / tools/list / tools/call over line-delimited JSON-RPC) that
hand-rolling it keeps the packaged binary small and avoids chasing SDK
breaking changes.
"""

import json
import os
import sys
import time
import traceback

from . import __version__, audit, db, directory as directory_module, goose, paths, policy, tokens

PROTOCOL_VERSION = "2024-11-05"


def _require_auth(principal):
    if not principal:
        raise PermissionError(
            "no valid AGENTICIAM_TOKEN was provided to this MCP server; "
            "issue one with `agenticiam token issue <identity>` and set it in the MCP client's env config"
        )


def _require_permission(principal, permission):
    _require_auth(principal)
    if not policy.is_allowed(principal.get("scopes", []), permission):
        raise PermissionError(f"principal {principal.get('name')!r} lacks permission {permission!r}")


def _actor(principal):
    return {"id": principal.get("sub"), "name": principal.get("name")}


# ---------------------------------------------------------------- tool handlers

def h_whoami(directory, principal, args):
    _require_auth(principal)
    return principal


def h_check_permission(directory, principal, args):
    _require_auth(principal)
    action = args["action"]
    return {"action": action, "allowed": policy.is_allowed(principal.get("scopes", []), action)}


def h_list_identities(directory, principal, args):
    _require_auth(principal)
    return directory.list_identities(kind=args.get("kind"))


def h_create_identity(directory, principal, args):
    _require_permission(principal, "iam:admin")
    return directory.create_identity(
        kind=args["kind"], name=args["name"], display_name=args.get("display_name"),
        actor=_actor(principal),
    )


def h_set_identity_enabled(directory, principal, args):
    _require_permission(principal, "iam:admin")
    return directory.set_enabled(args["name"], bool(args.get("enabled", True)), actor=_actor(principal))


def h_create_group(directory, principal, args):
    _require_permission(principal, "iam:admin")
    return directory.create_group(args["name"], args.get("description"), actor=_actor(principal))


def h_add_group_member(directory, principal, args):
    _require_permission(principal, "iam:admin")
    directory.add_member(args["group"], args["identity"], actor=_actor(principal))
    return {"ok": True}


def h_list_group_members(directory, principal, args):
    _require_auth(principal)
    return directory.list_members(args["group"])


def h_create_role(directory, principal, args):
    _require_permission(principal, "iam:admin")
    return directory.create_role(args["name"], args.get("description"), actor=_actor(principal))


def h_grant_role_permission(directory, principal, args):
    _require_permission(principal, "iam:admin")
    directory.grant_permission(args["role"], args["permission"], actor=_actor(principal))
    return {"ok": True}


def h_assign_role(directory, principal, args):
    _require_permission(principal, "iam:admin")
    directory.assign_role(args["role"], args["principal_type"], args["principal"], actor=_actor(principal))
    return {"ok": True}


def h_effective_permissions(directory, principal, args):
    _require_auth(principal)
    target = args.get("identity") or principal["name"]
    if target != principal["name"]:
        _require_permission(principal, "iam:admin")
    return sorted(directory.effective_permissions(target))


def h_issue_api_key(directory, principal, args):
    _require_permission(principal, "iam:admin")
    return directory.create_api_key(
        args["identity"], scopes=args.get("scopes"), ttl_seconds=args.get("ttl_seconds"), actor=_actor(principal)
    )


def h_audit_log(directory, principal, args):
    _require_permission(principal, "iam:admin")
    return audit.tail(directory.conn, limit=int(args.get("limit", 50)))


def h_dispatch_to_agent(directory, principal, args):
    """"Manager delegates a task to a worker" — requires dispatch:<agent>
    (or dispatch:*), only works against Ollama-backed workers since
    AgenticIAM never stores provider API keys to reconstruct a dispatch
    call for anything that needs one (see goose.run_agent_task)."""
    agent_name = args["agent"]
    _require_permission(principal, f"dispatch:{agent_name}")
    target = directory.get_identity(agent_name)
    goose_meta = (target.get("metadata") or {}).get("goose")
    if not goose_meta:
        raise ValueError(f"{agent_name!r} was not created as a Goose agent (no provider/model metadata)")
    if goose_meta.get("provider") != "ollama":
        raise ValueError(
            "dispatch currently only supports Ollama-backed workers — AgenticIAM never stores "
            "API keys for cloud providers, so there's nothing to dispatch a cloud-backed agent with"
        )
    timeout = args.get("timeout_seconds") or goose.DEFAULT_DISPATCH_TIMEOUT
    audit.log(
        directory.conn, f"dispatch.{agent_name}", "started",
        actor_id=principal.get("sub"), actor_name=principal.get("name"),
        detail={"provider": goose_meta["provider"], "model": goose_meta["model"], "timeout_seconds": timeout},
    )
    started = time.monotonic()
    try:
        response_text = goose.run_agent_task(
            goose_meta["provider"], goose_meta["model"], args["task"], timeout=timeout
        )
    except goose.DispatchError as exc:
        audit.log(
            directory.conn, f"dispatch.{agent_name}", "failure",
            actor_id=principal.get("sub"), actor_name=principal.get("name"),
            detail={"elapsed_seconds": round(time.monotonic() - started, 1), "error": str(exc)},
        )
        raise
    audit.log(
        directory.conn, f"dispatch.{agent_name}", "success",
        actor_id=principal.get("sub"), actor_name=principal.get("name"),
        detail={"elapsed_seconds": round(time.monotonic() - started, 1)},
    )
    return {"agent": agent_name, "response": response_text}


TOOL_HANDLERS = {
    "iam_whoami": h_whoami,
    "iam_check_permission": h_check_permission,
    "iam_list_identities": h_list_identities,
    "iam_create_identity": h_create_identity,
    "iam_set_identity_enabled": h_set_identity_enabled,
    "iam_create_group": h_create_group,
    "iam_add_group_member": h_add_group_member,
    "iam_list_group_members": h_list_group_members,
    "iam_create_role": h_create_role,
    "iam_grant_role_permission": h_grant_role_permission,
    "iam_assign_role": h_assign_role,
    "iam_effective_permissions": h_effective_permissions,
    "iam_issue_api_key": h_issue_api_key,
    "iam_audit_log": h_audit_log,
    "iam_dispatch_to_agent": h_dispatch_to_agent,
}

TOOL_SCHEMAS = [
    {
        "name": "iam_whoami",
        "description": "Return the identity, kind, and granted scopes for the calling MCP session's token.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "iam_check_permission",
        "description": "Check whether the calling identity's token grants a given permission string, e.g. 'shell:exec' or 'files:read'.",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "iam_list_identities",
        "description": "List directory identities (users, agents, services), optionally filtered by kind.",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["user", "agent", "service"]}},
        },
    },
    {
        "name": "iam_create_identity",
        "description": "Create a new identity in the directory (admin only). Returns a one-time secret for agent/service kinds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["user", "agent", "service"]},
                "name": {"type": "string"},
                "display_name": {"type": "string"},
            },
            "required": ["kind", "name"],
        },
    },
    {
        "name": "iam_set_identity_enabled",
        "description": "Enable or disable an identity, revoking its ability to authenticate (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
            "required": ["name", "enabled"],
        },
    },
    {
        "name": "iam_create_group",
        "description": "Create a directory group (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "iam_add_group_member",
        "description": "Add an identity to a group (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"group": {"type": "string"}, "identity": {"type": "string"}},
            "required": ["group", "identity"],
        },
    },
    {
        "name": "iam_list_group_members",
        "description": "List the identities that belong to a group.",
        "inputSchema": {
            "type": "object",
            "properties": {"group": {"type": "string"}},
            "required": ["group"],
        },
    },
    {
        "name": "iam_create_role",
        "description": "Create a role that permissions can be granted to and assigned from (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "iam_grant_role_permission",
        "description": "Grant a permission string (supports '*' and 'prefix:*' wildcards) to a role (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"role": {"type": "string"}, "permission": {"type": "string"}},
            "required": ["role", "permission"],
        },
    },
    {
        "name": "iam_assign_role",
        "description": "Assign a role to an identity or a group, granting its permissions (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "principal_type": {"type": "string", "enum": ["identity", "group"]},
                "principal": {"type": "string"},
            },
            "required": ["role", "principal_type", "principal"],
        },
    },
    {
        "name": "iam_effective_permissions",
        "description": "List the effective (role + group derived) permissions for an identity. Defaults to the caller; other identities require admin.",
        "inputSchema": {
            "type": "object",
            "properties": {"identity": {"type": "string"}},
        },
    },
    {
        "name": "iam_issue_api_key",
        "description": "Issue a long-lived, revocable API key for an identity, optionally scoped and time-limited (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identity": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "ttl_seconds": {"type": "integer"},
            },
            "required": ["identity"],
        },
    },
    {
        "name": "iam_audit_log",
        "description": "Tail the directory's audit log (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    {
        "name": "iam_dispatch_to_agent",
        "description": (
            "Delegate a task to another agent identity and get its text response back, like a manager "
            "dispatching to a worker. Requires the caller's token to grant dispatch:<agent> or dispatch:*. "
            "Only works against Ollama-backed agents created by the New Agent wizard — AgenticIAM never "
            "stores provider API keys, so cloud-backed agents (Anthropic/Google) can't be dispatched to."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "task": {"type": "string"},
                "timeout_seconds": {
                    "type": "number",
                    "description": "Defaults to 300 (matches the extension's own timeout). Raise it for a known-slow task.",
                },
            },
            "required": ["agent", "task"],
        },
    },
]


def _send(obj):
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except OSError:
        # The parent (Goose) closed its end of the stdio pipe — e.g. the
        # user closed the session/console this was spawned from. Nothing
        # left to talk to; exit quietly instead of crashing with a
        # traceback (this surfaced as "OSError: [Errno 22] Invalid
        # argument" on Windows when writing to a broken pipe).
        #
        # os._exit(), not sys.exit(): a plain sys.exit() still lets the
        # interpreter's normal shutdown sequence try to flush stdout one
        # more time, which hits the *same* broken pipe again — CPython
        # then overrides whatever exit code was requested with its own
        # hardcoded 120 ("failed to flush on exit") and prints an
        # "Exception ignored" notice to stderr. os._exit() skips shutdown
        # entirely (no atexit handlers, no stream flushing), guaranteeing
        # a clean, silent exit(0) instead of that leftover noise.
        os._exit(0)


def _handle_message(directory, principal, message):
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agenticiam", "version": __version__},
                },
            }
        )
        return

    if method in ("notifications/initialized", "notifications/cancelled"):
        return  # notifications get no response

    if method == "ping":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return

    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOL_SCHEMAS}})
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown tool {name!r}"}})
            return
        try:
            result = handler(directory, principal, arguments)
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "isError": False},
                }
            )
        except PermissionError as exc:
            _send(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
            )
        except (
            directory_module.NotFoundError, directory_module.ConflictError,
            goose.DispatchError, ValueError, KeyError,
        ) as exc:
            _send(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
            )
        except Exception:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": "internal error:\n" + traceback.format_exc()}], "isError": True},
                }
            )
        return

    if msg_id is not None:
        _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}})


def serve_stdio(db_path=None, in_stream=None):
    # `initialize`, `tools/list`, `ping`, and the `notifications/*` messages
    # are answered from pure in-memory constants (see _handle_message) and
    # must never wait on disk I/O — strict MCP clients (e.g. Goose) time out
    # a process that doesn't respond to `initialize` almost immediately.
    # So the directory (SQLite connect + schema) and AGENTICIAM_TOKEN
    # introspection are deferred until the first `tools/call`, which is the
    # earliest point they're actually needed. The stdio read loop itself
    # starts with zero setup work ahead of it.
    lazy = {"directory": None, "principal": None}

    def ensure_ready():
        if lazy["directory"] is not None:
            return
        conn = db.connect(db_path or paths.db_path())
        db.init_schema(conn)
        lazy["directory"] = directory_module.Directory(conn)
        token = os.environ.get("AGENTICIAM_TOKEN")
        if token:
            info = tokens.introspect(token, lazy["directory"])
            lazy["principal"] = info if info.get("active") else None

    stream = in_stream or sys.stdin
    try:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("method") == "tools/call":
                ensure_ready()
            _handle_message(lazy["directory"], lazy["principal"], message)
    except OSError:
        # Reading from a stdin pipe the parent already closed can raise
        # here too, depending on platform/timing — same story as the write
        # side in _send: skip normal interpreter shutdown (which would
        # try, and likely re-fail, one more stdout flush) via os._exit().
        os._exit(0)
