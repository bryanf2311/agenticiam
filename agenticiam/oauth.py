"""OAuth 2.0 client_credentials grant (RFC 6749 section 4.4) — the flow AI
agents and service gateways use to authenticate themselves — plus a plain
username/password login for human users, used by the web admin console
(there's deliberately no interactive OAuth authorization-code flow; see
README's Limitations section)."""

from . import audit, policy, tokens


class OAuthError(Exception):
    def __init__(self, error: str, description: str = None, status: int = 400):
        self.error = error
        self.description = description
        self.status = status
        super().__init__(f"{error}: {description}")


def client_credentials_grant(directory, client_id: str, client_secret: str, scope: str = None) -> dict:
    identity = directory.find_identity(client_id)
    if not identity or identity["kind"] not in ("agent", "service") or not identity["enabled"]:
        raise OAuthError("invalid_client", "unknown, wrong-kind, or disabled client", status=401)

    authenticated = directory.authenticate(identity["name"], client_secret)
    if not authenticated:
        raise OAuthError("invalid_client", "invalid client credentials", status=401)

    effective = directory.effective_permissions(identity["id"])

    if scope:
        requested = scope.split()
        granted = [s for s in requested if policy.is_allowed(effective, s)]
        if not granted:
            audit.log(
                directory.conn, "oauth.token", "denied",
                actor_id=identity["id"], actor_name=identity["name"],
                detail={"requested_scope": scope},
            )
            raise OAuthError("invalid_scope", "none of the requested scopes are granted")
    else:
        granted = sorted(effective)

    access_token = tokens.issue_access_token(identity, granted)
    audit.log(
        directory.conn, "oauth.token", "success",
        actor_id=identity["id"], actor_name=identity["name"],
        detail={"granted_scope": granted},
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": " ".join(granted),
    }


def password_login(directory, username: str, password: str) -> dict:
    identity = directory.find_identity(username)
    if not identity or identity["kind"] != "user" or not identity["enabled"]:
        raise OAuthError("invalid_grant", "unknown user or disabled account", status=401)

    authenticated = directory.authenticate(identity["name"], password)
    if not authenticated:
        raise OAuthError("invalid_grant", "invalid username or password", status=401)

    granted = sorted(directory.effective_permissions(identity["id"]))
    access_token = tokens.issue_access_token(identity, granted)
    audit.log(
        directory.conn, "login", "success",
        actor_id=identity["id"], actor_name=identity["name"],
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": " ".join(granted),
        "name": identity["name"],
    }
