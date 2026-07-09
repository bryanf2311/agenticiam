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
import traceback

from . import __version__, audit, db, directory as directory_module, paths, policy, tokens

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
]


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


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
        except (directory_module.NotFoundError, directory_module.ConflictError, ValueError, KeyError) as exc:
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
    conn = db.connect(db_path or paths.db_path())
    db.init_schema(conn)
    directory = directory_module.Directory(conn)

    token = os.environ.get("AGENTICIAM_TOKEN")
    principal = None
    if token:
        info = tokens.introspect(token, directory)
        principal = info if info.get("active") else None

    stream = in_stream or sys.stdin
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle_message(directory, principal, message)
