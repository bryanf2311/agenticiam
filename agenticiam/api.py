"""REST + OAuth2 surface.

This is the interface a gateway process (e.g. an OpenClaw instance) or any
plain HTTP-capable agent talks to: get a token, and before every privileged
action call POST /v1/authorize to ask "is this allowed?" — a policy
decision point in the same spirit as OPA or AWS IAM's Authorize call, just
scoped to a single self-hosted directory.
"""

import base64
import functools
import sys
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, request

from . import audit, db as db_module, directory as directory_module, goose, oauth, paths, policy, tokens

ADMIN_PERMISSION = "iam:admin"
BOOTSTRAP_ROLE = "domain-admin"


def _self_command():
    """Best-guess command+args to relaunch this same binary as an MCP
    server, for pre-filling the Goose extension wizard.

    Both shipped executables understand `mcp` as an argument — the GUI
    exe dispatches to the same CLI when given any argv (see gui.main) and
    only auto-launches the browser when run with none — so whichever one
    is actually running this server is the right thing to suggest.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ["mcp"]
    return sys.executable, ["-m", "agenticiam", "mcp"]


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["AGENTICIAM_DB_PATH"] = db_path or paths.db_path()

    def get_directory() -> directory_module.Directory:
        if "directory" not in g:
            conn = db_module.connect(app.config["AGENTICIAM_DB_PATH"])
            db_module.init_schema(conn)
            g.conn = conn
            g.directory = directory_module.Directory(conn)
        return g.directory

    @app.teardown_appcontext
    def _close_conn(exception=None):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    def _params() -> dict:
        data = {}
        if request.form:
            data.update(request.form)
        if request.is_json:
            data.update(request.get_json(silent=True) or {})
        data.update(request.args)
        return data

    def _bearer_token():
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):]
        return None

    def _client_credentials(data: dict):
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
                client_id, _, client_secret = decoded.partition(":")
                return client_id, client_secret
            except Exception:
                pass
        return data.get("client_id"), data.get("client_secret")

    def _current_principal():
        token = _bearer_token()
        if not token:
            return None
        info = tokens.introspect(token, get_directory())
        return info if info.get("active") else None

    def require_permission(permission):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                principal = _current_principal()
                if not principal:
                    return jsonify({"error": "unauthorized"}), 401
                if not policy.is_allowed(principal.get("scopes", []), permission):
                    audit.log(
                        get_directory().conn, f"authz.{permission}", "denied",
                        actor_id=principal.get("sub"), actor_name=principal.get("name"),
                    )
                    return jsonify({"error": "forbidden"}), 403
                g.principal = principal
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    def _actor():
        principal = getattr(g, "principal", None)
        if not principal:
            return None
        return {"id": principal.get("sub"), "name": principal.get("name")}

    # ------------------------------------------------------------ discovery
    @app.get("/.well-known/oauth-authorization-server")
    def oauth_metadata():
        base = request.host_url.rstrip("/")
        return jsonify(
            {
                "issuer": base,
                "token_endpoint": f"{base}/oauth/token",
                "introspection_endpoint": f"{base}/oauth/introspect",
                "revocation_endpoint": f"{base}/oauth/revoke",
                "grant_types_supported": ["client_credentials"],
                "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            }
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    # ------------------------------------------------------------ web admin console
    @app.get("/")
    def index():
        from . import webui

        return Response(webui.PAGE_HTML, mimetype="text/html")

    @app.get("/v1/setup/status")
    def setup_status():
        initialized = bool(get_directory().list_identities())
        return jsonify({"initialized": initialized})

    @app.post("/v1/setup/bootstrap")
    def setup_bootstrap():
        directory = get_directory()
        if directory.list_identities():
            return jsonify({"error": "already_initialized"}), 409
        data = request.get_json(force=True)
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or len(password) < 8:
            return (
                jsonify(
                    {
                        "error": "invalid_request",
                        "error_description": "username is required and password must be at least 8 characters",
                    }
                ),
                400,
            )
        try:
            role = directory.get_role(BOOTSTRAP_ROLE)
        except directory_module.NotFoundError:
            role = directory.create_role(BOOTSTRAP_ROLE, "Full directory administrator")
            directory.grant_permission(role["name"], "*")
        try:
            identity = directory.create_identity("user", username, display_name=username, secret=password)
        except directory_module.ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        directory.assign_role(role["name"], "identity", identity["name"])
        token_response = oauth.password_login(directory, username, password)
        return jsonify(token_response), 201

    @app.post("/v1/login")
    def login():
        data = _params()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return jsonify({"error": "invalid_request", "error_description": "username and password required"}), 400
        try:
            token_response = oauth.password_login(get_directory(), username, password)
        except oauth.OAuthError as exc:
            return jsonify({"error": exc.error, "error_description": exc.description}), exc.status
        return jsonify(token_response)

    # ------------------------------------------------------------ oauth2
    @app.post("/oauth/token")
    def oauth_token():
        data = _params()
        if data.get("grant_type") != "client_credentials":
            return jsonify({"error": "unsupported_grant_type"}), 400
        client_id, client_secret = _client_credentials(data)
        if not client_id or not client_secret:
            return jsonify({"error": "invalid_request", "error_description": "client_id and client_secret required"}), 400
        try:
            token_response = oauth.client_credentials_grant(get_directory(), client_id, client_secret, data.get("scope"))
        except oauth.OAuthError as exc:
            return jsonify({"error": exc.error, "error_description": exc.description}), exc.status
        return jsonify(token_response)

    @app.post("/oauth/introspect")
    def oauth_introspect():
        data = _params()
        token = data.get("token")
        if not token:
            return jsonify({"active": False}), 400
        return jsonify(tokens.introspect(token, get_directory()))

    @app.post("/oauth/revoke")
    def oauth_revoke():
        data = _params()
        token = data.get("token")
        if token and token.startswith("aiam_"):
            result = get_directory().verify_api_key(token)
            if result:
                get_directory().revoke_api_key(result["key_id"])
        return "", 200

    # ------------------------------------------------------------ authz + whoami
    @app.get("/v1/whoami")
    def whoami():
        principal = _current_principal()
        if not principal:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(principal)

    @app.post("/v1/authorize")
    def authorize():
        data = request.get_json(silent=True) or {}
        token = data.get("token") or _bearer_token()
        action = data.get("action")
        if not token or not action:
            return jsonify({"error": "invalid_request", "error_description": "token and action required"}), 400
        info = tokens.introspect(token, get_directory())
        if not info.get("active"):
            return jsonify({"allow": False, "reason": "inactive_token"})
        allowed = policy.is_allowed(info.get("scopes", []), action)
        audit.log(
            get_directory().conn, f"authz.{action}", "allowed" if allowed else "denied",
            actor_id=info.get("sub"), actor_name=info.get("name"), resource=data.get("resource"),
        )
        return jsonify({"allow": allowed, "subject": info.get("name"), "action": action})

    @app.post("/v1/agents/<name>/dispatch")
    def dispatch_to_agent(name):
        # Authorized by the caller's own dispatch:<name> (or dispatch:*)
        # scope rather than iam:admin — any agent granted that permission
        # (a "manager") can dispatch, not just directory admins.
        principal = _current_principal()
        if not principal:
            return jsonify({"error": "unauthorized"}), 401
        action = f"dispatch:{name}"
        directory = get_directory()
        if not policy.is_allowed(principal.get("scopes", []), action):
            audit.log(
                directory.conn, f"authz.{action}", "denied",
                actor_id=principal.get("sub"), actor_name=principal.get("name"),
            )
            return jsonify({"error": "forbidden"}), 403

        try:
            target = directory.get_identity(name)
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        goose_meta = (target.get("metadata") or {}).get("goose")
        if not goose_meta:
            return (
                jsonify(
                    {
                        "error": "invalid_request",
                        "error_description": f"{name!r} was not created as a Goose agent (no provider/model metadata)",
                    }
                ),
                400,
            )
        if goose_meta.get("provider") != "ollama":
            return (
                jsonify(
                    {
                        "error": "unsupported",
                        "error_description": (
                            "dispatch currently only supports Ollama-backed workers — AgenticIAM never stores "
                            "API keys for cloud providers, so there's nothing to dispatch a cloud-backed agent with"
                        ),
                    }
                ),
                400,
            )

        data = request.get_json(force=True)
        task = (data.get("task") or "").strip()
        if not task:
            return jsonify({"error": "invalid_request", "error_description": "task is required"}), 400
        timeout = data.get("timeout_seconds") or goose.DEFAULT_DISPATCH_TIMEOUT

        audit.log(
            directory.conn, f"dispatch.{name}", "started",
            actor_id=principal.get("sub"), actor_name=principal.get("name"),
            detail={"provider": goose_meta["provider"], "model": goose_meta["model"], "timeout_seconds": timeout},
        )
        started = time.monotonic()
        try:
            response_text = goose.run_agent_task(goose_meta["provider"], goose_meta["model"], task, timeout=timeout)
        except goose.DispatchError as exc:
            audit.log(
                directory.conn, f"dispatch.{name}", "failure",
                actor_id=principal.get("sub"), actor_name=principal.get("name"),
                detail={"elapsed_seconds": round(time.monotonic() - started, 1), "error": str(exc)},
            )
            return jsonify({"error": "dispatch_failed", "error_description": str(exc)}), 502

        audit.log(
            directory.conn, f"dispatch.{name}", "success",
            actor_id=principal.get("sub"), actor_name=principal.get("name"),
            detail={"elapsed_seconds": round(time.monotonic() - started, 1)},
        )
        return jsonify({"agent": name, "response": response_text})

    # ------------------------------------------------------------ admin: identities
    @app.get("/v1/admin/identities")
    @require_permission(ADMIN_PERMISSION)
    def list_identities():
        return jsonify(get_directory().list_identities(kind=request.args.get("kind")))

    @app.post("/v1/admin/identities")
    @require_permission(ADMIN_PERMISSION)
    def create_identity():
        data = request.get_json(force=True)
        try:
            identity = get_directory().create_identity(
                kind=data["kind"], name=data["name"], display_name=data.get("display_name"),
                secret=data.get("secret"), metadata=data.get("metadata"), actor=_actor(),
            )
        except directory_module.ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except (KeyError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(identity), 201

    @app.get("/v1/admin/identities/<name>")
    @require_permission(ADMIN_PERMISSION)
    def get_identity(name):
        try:
            return jsonify(get_directory().get_identity(name))
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.delete("/v1/admin/identities/<name>")
    @require_permission(ADMIN_PERMISSION)
    def delete_identity(name):
        directory = get_directory()
        actor = _actor()
        try:
            identity = directory.get_identity(name)
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        goose_meta = (identity.get("metadata") or {}).get("goose")

        directory.delete_identity(name, actor=actor)

        if not goose_meta:
            return "", 204

        # The identity is gone from the directory, but it may still be
        # registered as a Goose MCP extension in config.yaml, pointing at an
        # AGENTICIAM_TOKEN that no longer authenticates — clean that up too
        # rather than leaving a dead entry behind.
        extension_id = goose.slugify(name)
        result = {"identity": name, "extension_id": extension_id, "config_path": str(goose.config_path())}

        recipe_path = goose_meta.get("manager_recipe_path")
        if recipe_path:
            try:
                Path(recipe_path).unlink(missing_ok=True)
                result["manager_recipe_removed"] = True
            except OSError as exc:
                result["manager_recipe_removed"] = False
                result["manager_recipe_error"] = str(exc)

        try:
            cfg = goose.load_config()
            if extension_id in (cfg.get("extensions") or {}):
                cfg = goose.remove_extension(cfg, extension_id)
                written_path = goose.save_config(cfg)
                result["goose_config_updated"] = True
                result["config_path"] = str(written_path)
            else:
                result["goose_config_updated"] = False
                result["goose_config_note"] = "no matching extension entry was found in config.yaml"
        except OSError as exc:
            result["goose_config_updated"] = False
            result["goose_config_error"] = str(exc)
            result["manual_removal_instructions"] = (
                f"Open {result['config_path']} and delete the '{extension_id}:' block under 'extensions:'."
            )

        audit.log(
            directory.conn, "goose.cleanup_extension",
            "success" if result.get("goose_config_updated") else "partial",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{name}",
        )
        return jsonify(result), 200

    @app.post("/v1/admin/identities/<name>/enabled")
    @require_permission(ADMIN_PERMISSION)
    def set_identity_enabled(name):
        data = request.get_json(force=True)
        try:
            identity = get_directory().set_enabled(name, bool(data.get("enabled", True)), actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(identity)

    @app.post("/v1/admin/identities/<name>/rotate-secret")
    @require_permission(ADMIN_PERMISSION)
    def rotate_secret(name):
        try:
            new_secret = get_directory().rotate_secret(name, actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"secret": new_secret})

    @app.get("/v1/admin/identities/<name>/permissions")
    @require_permission(ADMIN_PERMISSION)
    def identity_permissions(name):
        try:
            return jsonify(sorted(get_directory().effective_permissions(name)))
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

    # ------------------------------------------------------------ admin: groups
    @app.get("/v1/admin/groups")
    @require_permission(ADMIN_PERMISSION)
    def list_groups():
        return jsonify(get_directory().list_groups())

    @app.post("/v1/admin/groups")
    @require_permission(ADMIN_PERMISSION)
    def create_group():
        data = request.get_json(force=True)
        try:
            group = get_directory().create_group(data["name"], data.get("description"), actor=_actor())
        except directory_module.ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(group), 201

    @app.get("/v1/admin/groups/<name>/members")
    @require_permission(ADMIN_PERMISSION)
    def list_group_members(name):
        try:
            return jsonify(get_directory().list_members(name))
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/v1/admin/groups/<name>/members")
    @require_permission(ADMIN_PERMISSION)
    def add_group_member(name):
        data = request.get_json(force=True)
        try:
            get_directory().add_member(name, data["identity"], actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return "", 204

    @app.delete("/v1/admin/groups/<name>/members/<identity_name>")
    @require_permission(ADMIN_PERMISSION)
    def remove_group_member(name, identity_name):
        try:
            get_directory().remove_member(name, identity_name, actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return "", 204

    @app.get("/v1/admin/groups/<name>/launch-commands")
    @require_permission(ADMIN_PERMISSION)
    def group_launch_commands(name):
        try:
            members = get_directory().list_members(name)
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        agents = []
        for member in members:
            goose_meta = (member.get("metadata") or {}).get("goose")
            commands = None
            if goose_meta and goose_meta.get("provider") and goose_meta.get("model"):
                commands = goose.launch_commands(
                    member["name"], goose_meta["provider"], goose_meta["model"],
                    context_limit=goose_meta.get("context_limit"),
                    recipe_path=goose_meta.get("manager_recipe_path"),
                )
            agents.append(
                {"name": member["name"], "kind": member["kind"], "goose": goose_meta, "launch_commands": commands}
            )
        return jsonify({"group": name, "agents": agents})

    # ------------------------------------------------------------ admin: roles
    @app.get("/v1/admin/roles")
    @require_permission(ADMIN_PERMISSION)
    def list_roles():
        return jsonify(get_directory().list_roles())

    @app.post("/v1/admin/roles")
    @require_permission(ADMIN_PERMISSION)
    def create_role():
        data = request.get_json(force=True)
        try:
            role = get_directory().create_role(data["name"], data.get("description"), actor=_actor())
        except directory_module.ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(role), 201

    @app.get("/v1/admin/roles/<name>/permissions")
    @require_permission(ADMIN_PERMISSION)
    def list_role_permissions(name):
        try:
            return jsonify(get_directory().list_role_permissions(name))
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/v1/admin/roles/<name>/permissions")
    @require_permission(ADMIN_PERMISSION)
    def grant_role_permission(name):
        data = request.get_json(force=True)
        try:
            get_directory().grant_permission(name, data["permission"], actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return "", 204

    @app.delete("/v1/admin/roles/<name>/permissions/<path:permission>")
    @require_permission(ADMIN_PERMISSION)
    def revoke_role_permission(name, permission):
        try:
            get_directory().revoke_permission(name, permission, actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return "", 204

    @app.post("/v1/admin/roles/<name>/assignments")
    @require_permission(ADMIN_PERMISSION)
    def assign_role(name):
        data = request.get_json(force=True)
        try:
            get_directory().assign_role(name, data["principal_type"], data["principal"], actor=_actor())
        except (directory_module.NotFoundError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return "", 204

    @app.delete("/v1/admin/roles/<name>/assignments/<principal_type>/<principal>")
    @require_permission(ADMIN_PERMISSION)
    def unassign_role(name, principal_type, principal):
        try:
            get_directory().unassign_role(name, principal_type, principal, actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return "", 204

    # ------------------------------------------------------------ admin: api keys
    @app.post("/v1/admin/identities/<name>/api-keys")
    @require_permission(ADMIN_PERMISSION)
    def create_api_key(name):
        data = request.get_json(silent=True) or {}
        try:
            key = get_directory().create_api_key(
                name, scopes=data.get("scopes"), ttl_seconds=data.get("ttl_seconds"), actor=_actor()
            )
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(key), 201

    @app.get("/v1/admin/identities/<name>/api-keys")
    @require_permission(ADMIN_PERMISSION)
    def list_api_keys(name):
        try:
            return jsonify(get_directory().list_api_keys(name))
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.delete("/v1/admin/api-keys/<key_id>")
    @require_permission(ADMIN_PERMISSION)
    def revoke_api_key(key_id):
        get_directory().revoke_api_key(key_id, actor=_actor())
        return "", 204

    # ------------------------------------------------------------ admin: goose / ollama "new agent" wizard
    @app.get("/v1/admin/goose/status")
    @require_permission(ADMIN_PERMISSION)
    def goose_status():
        cmd, args = _self_command()
        try:
            goose.list_ollama_models()
            ollama_reachable = True
        except goose.OllamaUnavailable:
            ollama_reachable = False
        return jsonify(
            {
                "goose_installed": goose.find_goose_binary() is not None,
                "goose_path": goose.find_goose_binary(),
                "ollama_installed": goose.find_ollama_binary() is not None,
                "ollama_reachable": ollama_reachable,
                "config_path": str(goose.config_path()),
                "suggested_cmd": cmd,
                "suggested_args": args,
            }
        )

    @app.post("/v1/admin/goose/models")
    @require_permission(ADMIN_PERMISSION)
    def goose_models():
        data = request.get_json(silent=True) or {}
        provider = (data.get("provider") or "ollama").strip()
        api_key = data.get("api_key") or None
        try:
            models = goose.list_provider_models(provider, api_key=api_key)
        except goose.ProviderUnavailable as exc:
            return jsonify({"available": False, "models": [], "error": str(exc)})
        except ValueError as exc:
            return jsonify({"error": "invalid_request", "error_description": str(exc)}), 400
        return jsonify({"available": True, "models": models})

    @app.post("/v1/admin/goose/agents")
    @require_permission(ADMIN_PERMISSION)
    def goose_create_agent():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "invalid_request", "error_description": "name is required"}), 400
        permissions = [p.strip() for p in (data.get("permissions") or []) if p and p.strip()]
        model = (data.get("model") or "").strip() or None
        provider = (data.get("provider") or "ollama").strip()
        # api_key is used only to build the launch command below — it is
        # never written to config.yaml or stored in the directory, matching
        # Goose's own guidance against keeping provider keys in plaintext files.
        api_key = data.get("api_key") or None
        context_limit = data.get("context_limit")
        try:
            context_limit = int(context_limit) if context_limit else None
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_request", "error_description": "context_limit must be an integer"}), 400
        set_as_default = bool(data.get("set_as_default"))
        group_name = (data.get("group") or "").strip() or None
        cmd = data.get("cmd")
        args = data.get("args")
        if not cmd:
            cmd, args = _self_command()
        args = args or ["mcp"]

        # A "manager" isn't a hardcoded role — it's just an agent that was
        # granted dispatch: permissions in the wizard's "Manager
        # permissions" step. That's also the signal for preloading the
        # dispatch system prompt below.
        is_manager = any(p == "dispatch:*" or p.startswith("dispatch:") for p in permissions)
        recipe_path = None
        recipe_error = None
        if is_manager:
            try:
                recipe_path = goose.write_manager_recipe(name)
            except OSError as exc:
                recipe_error = str(exc)

        directory = get_directory()
        actor = _actor()
        role_name = f"{name}-role"
        try:
            try:
                role = directory.create_role(role_name, f"Permissions for Goose agent {name}", actor=actor)
            except directory_module.ConflictError:
                role = directory.get_role(role_name)
            for perm in permissions:
                directory.grant_permission(role["name"], perm, actor=actor)
            identity = directory.create_identity(
                "agent", name, display_name=name,
                metadata={"goose": {
                    "provider": provider, "model": model, "context_limit": context_limit,
                    "is_manager": is_manager,
                    "manager_recipe_path": str(recipe_path) if recipe_path else None,
                }},
                actor=actor,
            )
            directory.assign_role(role["name"], "identity", identity["name"], actor=actor)
            key = directory.create_api_key(identity["name"], scopes=None, actor=actor)
            if group_name:
                try:
                    group = directory.create_group(group_name, actor=actor)
                except directory_module.ConflictError:
                    group = directory.get_group(group_name)
                directory.add_member(group["name"], identity["name"], actor=actor)
        except directory_module.ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except (directory_module.NotFoundError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        extension_id = goose.slugify(name)
        # once set_as_default writes GOOSE_PROVIDER/GOOSE_MODEL/GOOSE_CONTEXT_LIMIT into
        # config.yaml, a per-invocation env override would just be redundant
        launch_commands = goose.launch_commands(
            name,
            provider,
            None if set_as_default else model,
            context_limit=None if set_as_default else context_limit,
            api_key=api_key,
            recipe_path=recipe_path,
        )

        result = {
            "identity": identity,
            "role": role_name,
            "permissions": permissions,
            "api_key_id": key["id"],
            "extension_id": extension_id,
            "config_path": str(goose.config_path()),
            "launch_commands": launch_commands,
            "group": group_name,
            "is_manager": is_manager,
            "manager_recipe_path": str(recipe_path) if recipe_path else None,
            "manager_recipe_error": recipe_error,
        }

        try:
            cfg = goose.load_config()
            cfg = goose.register_extension(cfg, extension_id, name, cmd, args, key["key"])
            if set_as_default and model:
                cfg = goose.set_default_provider_model(cfg, provider, model, context_limit=context_limit)
            written_path = goose.save_config(cfg)
            result["goose_config_written"] = True
            result["config_path"] = str(written_path)
        except OSError as exc:
            result["goose_config_written"] = False
            result["goose_config_error"] = str(exc)
            result["manual_extension_snippet"] = goose.extension_snippet_yaml(extension_id, name, cmd, args, key["key"])

        audit.log(
            directory.conn, "goose.create_agent",
            "success" if result.get("goose_config_written") else "partial",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{name}",
            detail={"provider": provider, "model": model, "permissions": permissions, "context_limit": context_limit},
        )
        return jsonify(result), 201

    # ------------------------------------------------------------ admin: audit
    @app.get("/v1/admin/audit")
    @require_permission(ADMIN_PERMISSION)
    def audit_log():
        limit = int(request.args.get("limit", 50))
        return jsonify(audit.tail(get_directory().conn, limit=limit))

    return app
