import pytest

from agenticiam import db as db_module, tokens
from agenticiam.api import create_app
from agenticiam.directory import Directory


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICIAM_HOME", str(tmp_path))
    path = tmp_path / "test.db"
    conn = db_module.connect(path)
    db_module.init_schema(conn)
    conn.close()
    return path


@pytest.fixture
def app(db_path):
    flask_app = create_app(db_path=db_path)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def directory(db_path):
    conn = db_module.connect(db_path)
    return Directory(conn)


@pytest.fixture
def admin_token(directory):
    directory.create_role("admin-role")
    directory.grant_permission("admin-role", "*")
    identity = directory.create_identity("user", "admin")
    directory.assign_role("admin-role", "identity", "admin")
    return tokens.issue_access_token(identity, ["*"])


def test_healthz(client):
    assert client.get("/healthz").status_code == 200


def test_oauth_metadata_discovery(client):
    resp = client.get("/.well-known/oauth-authorization-server")
    body = resp.get_json()
    assert body["token_endpoint"].endswith("/oauth/token")


def test_oauth_client_credentials_flow(client, directory):
    directory.create_role("agent-role")
    directory.grant_permission("agent-role", "shell:exec")
    identity = directory.create_identity("agent", "bot1")
    directory.assign_role("agent-role", "identity", "bot1")

    resp = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "bot1", "client_secret": identity["secret"]},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "shell:exec"
    token = body["access_token"]

    intro = client.post("/oauth/introspect", data={"token": token})
    assert intro.get_json()["active"] is True


def test_oauth_basic_auth_client_credentials(client, directory):
    import base64

    identity = directory.create_identity("agent", "bot1")
    creds = base64.b64encode(f"bot1:{identity['secret']}".encode()).decode()
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert resp.status_code == 200


def test_oauth_wrong_secret_rejected(client, directory):
    directory.create_identity("agent", "bot1")
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "bot1", "client_secret": "wrong"},
    )
    assert resp.status_code == 401


def test_oauth_user_kind_cannot_use_client_credentials(client, directory):
    directory.create_identity("user", "alice", secret="pw")
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "alice", "client_secret": "pw"},
    )
    assert resp.status_code == 401


def test_authorize_endpoint(client, directory):
    directory.create_role("r")
    directory.grant_permission("r", "files:*")
    identity = directory.create_identity("agent", "bot1")
    directory.assign_role("r", "identity", "bot1")
    token = tokens.issue_access_token(identity, ["files:*"])

    allowed = client.post("/v1/authorize", json={"token": token, "action": "files:read"})
    assert allowed.get_json()["allow"] is True

    denied = client.post("/v1/authorize", json={"token": token, "action": "shell:exec"})
    assert denied.get_json()["allow"] is False


def test_admin_endpoints_require_admin_scope(client, directory):
    identity = directory.create_identity("agent", "no-admin")
    token = tokens.issue_access_token(identity, [])
    resp = client.get("/v1/admin/identities", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_admin_endpoints_unauthorized_without_token(client):
    assert client.get("/v1/admin/identities").status_code == 401


def test_admin_create_and_list_identity(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    resp = client.post("/v1/admin/identities", json={"kind": "agent", "name": "bot2"}, headers=headers)
    assert resp.status_code == 201
    assert "secret" in resp.get_json()
    names = [i["name"] for i in client.get("/v1/admin/identities", headers=headers).get_json()]
    assert "bot2" in names


def test_admin_group_and_role_flow(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    client.post("/v1/admin/identities", json={"kind": "user", "name": "alice"}, headers=headers)
    client.post("/v1/admin/groups", json={"name": "eng"}, headers=headers)
    client.post("/v1/admin/groups/eng/members", json={"identity": "alice"}, headers=headers)
    members = client.get("/v1/admin/groups/eng/members", headers=headers).get_json()
    assert [m["name"] for m in members] == ["alice"]

    client.post("/v1/admin/roles", json={"name": "eng-role"}, headers=headers)
    client.post("/v1/admin/roles/eng-role/permissions", json={"permission": "files:*"}, headers=headers)
    client.post(
        "/v1/admin/roles/eng-role/assignments",
        json={"principal_type": "group", "principal": "eng"},
        headers=headers,
    )

    perms = client.get("/v1/admin/identities/alice/permissions", headers=headers).get_json()
    assert perms == ["files:*"]


def test_admin_duplicate_identity_conflict(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    client.post("/v1/admin/identities", json={"kind": "agent", "name": "dup"}, headers=headers)
    resp = client.post("/v1/admin/identities", json={"kind": "agent", "name": "dup"}, headers=headers)
    assert resp.status_code == 409


def test_api_key_issue_and_revoke(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    client.post("/v1/admin/identities", json={"kind": "agent", "name": "bot4"}, headers=headers)
    resp = client.post("/v1/admin/identities/bot4/api-keys", json={"scopes": ["files:read"]}, headers=headers)
    assert resp.status_code == 201
    key = resp.get_json()["key"]

    whoami = client.get("/v1/whoami", headers={"Authorization": f"Bearer {key}"})
    assert whoami.get_json()["active"] is True

    revoke = client.post("/oauth/revoke", data={"token": key})
    assert revoke.status_code == 200
    whoami2 = client.get("/v1/whoami", headers={"Authorization": f"Bearer {key}"})
    assert whoami2.status_code == 401


def test_audit_log_endpoint(client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    client.post("/v1/admin/identities", json={"kind": "agent", "name": "bot3"}, headers=headers)
    entries = client.get("/v1/admin/audit?limit=5", headers=headers).get_json()
    assert len(entries) > 0
