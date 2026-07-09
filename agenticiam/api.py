"""REST + OAuth2 surface.

This is the interface a gateway process (e.g. an OpenClaw instance) or any
plain HTTP-capable agent talks to: get a token, and before every privileged
action call POST /v1/authorize to ask "is this allowed?" — a policy
decision point in the same spirit as OPA or AWS IAM's Authorize call, just
scoped to a single self-hosted directory.
"""

import base64
import functools

from flask import Flask, Response, g, jsonify, request

from . import audit, db as db_module, directory as directory_module, oauth, paths, policy, tokens

ADMIN_PERMISSION = "iam:admin"
BOOTSTRAP_ROLE = "domain-admin"


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
        try:
            get_directory().delete_identity(name, actor=_actor())
        except directory_module.NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return "", 204

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

    # ------------------------------------------------------------ admin: audit
    @app.get("/v1/admin/audit")
    @require_permission(ADMIN_PERMISSION)
    def audit_log():
        limit = int(request.args.get("limit", 50))
        return jsonify(audit.tail(get_directory().conn, limit=limit))

    return app
