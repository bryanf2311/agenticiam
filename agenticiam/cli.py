"""Admin CLI — the AgenticIAM equivalent of AD's dsadd/dsquery/net user,
run locally against the directory file with trusted OS-level access (no
network auth required, same trust model as running tools on the DC itself).
"""

import json
import time

import click

from . import __version__
from . import audit as audit_module
from . import crypto, db, paths, policy, tokens
from .directory import ConflictError, Directory, NotFoundError


def _directory() -> Directory:
    conn = db.connect()
    db.init_schema(conn)
    return Directory(conn)


@click.group()
@click.version_option(version=__version__)
def main():
    """AgenticIAM: a self-hosted directory, RBAC, and auth gateway for AI agents."""


@main.command()
@click.option("--force", is_flag=True, help="Reset the admin secret if the admin identity already exists.")
def init(force):
    """Initialize the directory: create the DB, signing key, and a bootstrap admin identity."""
    d = _directory()
    tokens.load_signing_key()  # ensure the signing key exists up front

    try:
        role = d.get_role("domain-admin")
    except NotFoundError:
        role = d.create_role("domain-admin", "Full directory administrator")
        d.grant_permission(role["name"], "*")

    existing = d.find_identity("admin")
    if existing and not force:
        click.echo(f"Already initialized ({paths.db_path()}). Identity 'admin' exists; use --force to reset its secret.")
        return

    if existing:
        secret = d.rotate_secret("admin")
    else:
        secret = crypto.generate_secret()
        identity = d.create_identity("user", "admin", display_name="Directory Administrator", secret=secret)
        d.assign_role(role["name"], "identity", identity["name"])

    click.echo(f"AgenticIAM directory initialized at {paths.db_path()}")
    click.echo("Admin identity: admin")
    click.echo(f"Admin secret (save this now, it will not be shown again): {secret}")
    click.echo("\nGet a token with:  agenticiam token issue admin")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
def serve(host, port):
    """Run the REST + OAuth2 directory server."""
    from .api import create_app

    app = create_app()
    click.echo(f"AgenticIAM directory server listening on http://{host}:{port}")
    app.run(host=host, port=port)


@main.command()
def mcp():
    """Run the MCP stdio server (for Claude Desktop / Claude Code / Claude Cowork). Reads AGENTICIAM_TOKEN from env."""
    from .mcp_server import serve_stdio

    serve_stdio()


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--no-browser", is_flag=True, help="Don't automatically open a browser window.")
def gui(host, port, no_browser):
    """Launch the web admin console: starts the server and opens it in your browser."""
    from .gui import launch_gui

    click.echo(f"AgenticIAM admin console: http://{host}:{port}/")
    launch_gui(host=host, port=port, open_browser=not no_browser)


@main.command()
@click.option("--token", "token_value", envvar="AGENTICIAM_TOKEN", required=True)
def whoami(token_value):
    """Show the identity and scopes a token resolves to."""
    d = _directory()
    click.echo(json.dumps(tokens.introspect(token_value, d), indent=2))


# ---------------------------------------------------------------- users
@main.group()
def user():
    """Human user identities."""


@user.command("add")
@click.argument("name")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@click.option("--display-name", default=None)
def user_add(name, password, display_name):
    d = _directory()
    try:
        identity = d.create_identity("user", name, display_name=display_name, secret=password)
    except ConflictError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Created user {identity['name']} ({identity['id']})")


@user.command("list")
def user_list():
    d = _directory()
    for i in d.list_identities(kind="user"):
        click.echo(f"{i['name']}\t{'enabled' if i['enabled'] else 'disabled'}")


# ---------------------------------------------------------------- agents / services
@main.group()
def agent():
    """AI agent / service identities (OAuth2 client_credentials clients)."""


@agent.command("add")
@click.argument("name")
@click.option("--kind", type=click.Choice(["agent", "service"]), default="agent", show_default=True)
@click.option("--display-name", default=None)
def agent_add(name, kind, display_name):
    d = _directory()
    try:
        identity = d.create_identity(kind, name, display_name=display_name)
    except ConflictError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Created {kind} {identity['name']} ({identity['id']})")
    click.echo(f"client_id:     {identity['name']}")
    click.echo(f"client_secret (save now, shown once): {identity['secret']}")


@agent.command("list")
def agent_list():
    d = _directory()
    for i in d.list_identities():
        if i["kind"] in ("agent", "service"):
            click.echo(f"{i['name']}\t{i['kind']}\t{'enabled' if i['enabled'] else 'disabled'}")


@agent.command("rotate-secret")
@click.argument("name")
def agent_rotate_secret(name):
    d = _directory()
    try:
        secret = d.rotate_secret(name)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"New client_secret (shown once): {secret}")


@agent.command("dispatch")
@click.argument("name")
@click.argument("task")
@click.option("--timeout", default=None, type=float, help="Seconds to wait (default 300, matches the extension's own MCP timeout).")
def agent_dispatch(name, task, timeout):
    """Run a one-shot task through a Goose-linked agent (must be Ollama-backed; see the New Agent wizard docs)."""
    d = _directory()
    try:
        target = d.get_identity(name)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    goose_meta = (target.get("metadata") or {}).get("goose")
    if not goose_meta:
        raise click.ClickException(f"{name!r} was not created as a Goose agent (no provider/model metadata)")
    if goose_meta.get("provider") != "ollama":
        raise click.ClickException(
            "dispatch currently only supports Ollama-backed workers — AgenticIAM never stores "
            "API keys for cloud providers"
        )
    from . import goose as goose_module

    effective_timeout = timeout or goose_module.DEFAULT_DISPATCH_TIMEOUT
    audit_module.log(
        d.conn, f"dispatch.{name}", "started", actor_name="cli",
        detail={"provider": goose_meta["provider"], "model": goose_meta["model"], "timeout_seconds": effective_timeout},
    )
    started = time.monotonic()
    try:
        result = goose_module.run_agent_task(goose_meta["provider"], goose_meta["model"], task, timeout=effective_timeout)
    except goose_module.DispatchError as exc:
        audit_module.log(
            d.conn, f"dispatch.{name}", "failure", actor_name="cli",
            detail={"elapsed_seconds": round(time.monotonic() - started, 1), "error": str(exc)},
        )
        raise click.ClickException(str(exc))
    audit_module.log(
        d.conn, f"dispatch.{name}", "success", actor_name="cli",
        detail={"elapsed_seconds": round(time.monotonic() - started, 1)},
    )
    click.echo(result)


# ---------------------------------------------------------------- generic identity ops
@main.group()
def identity():
    """Operations that apply to any identity kind."""


@identity.command("show")
@click.argument("name")
def identity_show(name):
    d = _directory()
    try:
        click.echo(json.dumps(d.get_identity(name), indent=2))
    except NotFoundError as exc:
        raise click.ClickException(str(exc))


@identity.command("enable")
@click.argument("name")
def identity_enable(name):
    d = _directory()
    try:
        d.set_enabled(name, True)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Enabled {name}")


@identity.command("disable")
@click.argument("name")
def identity_disable(name):
    d = _directory()
    try:
        d.set_enabled(name, False)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Disabled {name}")


@identity.command("rm")
@click.argument("name")
def identity_rm(name):
    d = _directory()
    try:
        d.delete_identity(name)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Deleted {name}")


@identity.command("permissions")
@click.argument("name")
def identity_permissions(name):
    d = _directory()
    try:
        for p in sorted(d.effective_permissions(name)):
            click.echo(p)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))


# ---------------------------------------------------------------- groups
@main.group()
def group():
    """Groups (like AD security groups) — attach roles here to grant permissions to everyone in the group."""


@group.command("add")
@click.argument("name")
@click.option("--description", default=None)
def group_add(name, description):
    d = _directory()
    try:
        g = d.create_group(name, description)
    except ConflictError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Created group {g['name']}")


@group.command("list")
def group_list():
    d = _directory()
    for g in d.list_groups():
        click.echo(g["name"])


@group.command("add-member")
@click.argument("group_name")
@click.argument("identity_name")
def group_add_member(group_name, identity_name):
    d = _directory()
    try:
        d.add_member(group_name, identity_name)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Added {identity_name} to {group_name}")


@group.command("remove-member")
@click.argument("group_name")
@click.argument("identity_name")
def group_remove_member(group_name, identity_name):
    d = _directory()
    try:
        d.remove_member(group_name, identity_name)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Removed {identity_name} from {group_name}")


@group.command("members")
@click.argument("group_name")
def group_members(group_name):
    d = _directory()
    try:
        for i in d.list_members(group_name):
            click.echo(i["name"])
    except NotFoundError as exc:
        raise click.ClickException(str(exc))


# ---------------------------------------------------------------- roles
@main.group()
def role():
    """Roles bundle permissions and can be assigned to identities or groups."""


@role.command("add")
@click.argument("name")
@click.option("--description", default=None)
def role_add(name, description):
    d = _directory()
    try:
        r = d.create_role(name, description)
    except ConflictError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Created role {r['name']}")


@role.command("list")
def role_list():
    d = _directory()
    for r in d.list_roles():
        click.echo(r["name"])


@role.command("permissions")
@click.argument("role_name")
def role_permissions(role_name):
    d = _directory()
    try:
        for p in d.list_role_permissions(role_name):
            click.echo(p)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))


@role.command("grant")
@click.argument("role_name")
@click.argument("permission")
def role_grant(role_name, permission):
    d = _directory()
    try:
        d.grant_permission(role_name, permission)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Granted {permission!r} to role {role_name}")


@role.command("revoke")
@click.argument("role_name")
@click.argument("permission")
def role_revoke(role_name, permission):
    d = _directory()
    try:
        d.revoke_permission(role_name, permission)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Revoked {permission!r} from role {role_name}")


@role.command("assign")
@click.argument("role_name")
@click.argument("principal_type", type=click.Choice(["identity", "group"]))
@click.argument("principal")
def role_assign(role_name, principal_type, principal):
    d = _directory()
    try:
        d.assign_role(role_name, principal_type, principal)
    except (NotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Assigned role {role_name} to {principal_type} {principal}")


@role.command("unassign")
@click.argument("role_name")
@click.argument("principal_type", type=click.Choice(["identity", "group"]))
@click.argument("principal")
def role_unassign(role_name, principal_type, principal):
    d = _directory()
    try:
        d.unassign_role(role_name, principal_type, principal)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Unassigned role {role_name} from {principal_type} {principal}")


# ---------------------------------------------------------------- tokens
@main.group()
def token():
    """Issue and inspect access tokens."""


@token.command("issue")
@click.argument("identity_name")
@click.option("--scope", "scopes", multiple=True, help="Restrict to specific scopes (repeatable). Defaults to all granted permissions.")
@click.option("--ttl", default=3600, show_default=True, help="Lifetime in seconds.")
def token_issue(identity_name, scopes, ttl):
    d = _directory()
    try:
        identity = d.get_identity(identity_name)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    effective = d.effective_permissions(identity["id"])
    if scopes:
        granted = [s for s in scopes if policy.is_allowed(effective, s)]
        if not granted:
            raise click.ClickException("none of the requested scopes are granted to this identity")
    else:
        granted = sorted(effective)
    access_token = tokens.issue_access_token(identity, granted, ttl_seconds=ttl)
    audit_module.log(
        d.conn, "cli.token_issue", "success", actor_name="cli",
        resource=f"identity:{identity_name}", detail={"scopes": granted},
    )
    click.echo(access_token)


@token.command("introspect")
@click.argument("token_value")
def token_introspect(token_value):
    d = _directory()
    click.echo(json.dumps(tokens.introspect(token_value, d), indent=2))


@main.group(name="api-key")
def api_key():
    """Long-lived, revocable API keys (an alternative to short-lived OAuth tokens)."""


@api_key.command("issue")
@click.argument("identity_name")
@click.option("--scope", "scopes", multiple=True)
@click.option("--ttl", default=None, type=int, help="Lifetime in seconds; omit for no expiry.")
def api_key_issue(identity_name, scopes, ttl):
    d = _directory()
    try:
        key = d.create_api_key(identity_name, scopes=list(scopes) or None, ttl_seconds=ttl)
    except NotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"API key (shown once): {key['key']}")


@api_key.command("revoke")
@click.argument("key_id")
def api_key_revoke(key_id):
    d = _directory()
    d.revoke_api_key(key_id)
    click.echo(f"Revoked {key_id}")


@api_key.command("list")
@click.argument("identity_name", required=False)
def api_key_list(identity_name):
    d = _directory()
    for k in d.list_api_keys(identity_name):
        status = "revoked" if k["revoked"] else "active"
        click.echo(f"{k['id']}\t{status}\texpires={k['expires_at']}\tlast_used={k['last_used_at']}")


# ---------------------------------------------------------------- audit
@main.group(name="audit")
def audit_group():
    """Inspect the audit log."""


@audit_group.command("tail")
@click.option("-n", "--limit", default=50, show_default=True)
def audit_tail(limit):
    d = _directory()
    for entry in audit_module.tail(d.conn, limit=limit):
        click.echo(json.dumps(entry))


if __name__ == "__main__":
    main()
