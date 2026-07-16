import pytest

from agenticiam import db as db_module, openclaw, tokens
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


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def test_list_agents_reports_not_installed(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "find_openclaw_binary", lambda: None)
    resp = client.get("/v1/admin/openclaw/agents", headers=admin_headers)
    body = resp.get_json()
    assert body == {"available": False, "agents": [], "error": "openclaw is not installed on this server"}


def test_list_agents_requires_admin(client, directory):
    identity = directory.create_identity("agent", "bot1")
    token = tokens.issue_access_token(identity, [])
    resp = client.get("/v1/admin/openclaw/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_list_agents_returns_live_list(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "find_openclaw_binary", lambda: "/usr/bin/openclaw")
    monkeypatch.setattr(openclaw, "list_agents", lambda: [{"id": "boss"}, {"id": "intern"}])
    resp = client.get("/v1/admin/openclaw/agents", headers=admin_headers)
    body = resp.get_json()
    assert body == {"available": True, "agents": [{"id": "boss"}, {"id": "intern"}]}


def test_list_agents_surfaces_cli_error(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "find_openclaw_binary", lambda: "/usr/bin/openclaw")

    def raise_error():
        raise openclaw.OpenClawCliError("gateway not running")

    monkeypatch.setattr(openclaw, "list_agents", raise_error)
    resp = client.get("/v1/admin/openclaw/agents", headers=admin_headers)
    body = resp.get_json()
    assert body == {"available": False, "agents": [], "error": "gateway not running"}


def test_get_agent_returns_config(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "get_agent_config", lambda agent_id: {"id": agent_id, "tools": {"allow": ["read"]}})
    resp = client.get("/v1/admin/openclaw/agents/boss", headers=admin_headers)
    assert resp.get_json() == {"id": "boss", "tools": {"allow": ["read"]}}


def test_get_agent_not_found_returns_502_with_detail(client, admin_headers, monkeypatch):
    def raise_error(agent_id):
        raise openclaw.OpenClawCliError(f"no OpenClaw agent with id {agent_id!r}")

    monkeypatch.setattr(openclaw, "get_agent_config", raise_error)
    resp = client.get("/v1/admin/openclaw/agents/nope", headers=admin_headers)
    assert resp.status_code == 502
    assert "nope" in resp.get_json()["error_description"]


def test_set_agent_permissions_writes_only_provided_fields(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(openclaw, "set_agent_tools", lambda agent_id, allow=None, deny=None: calls.setdefault("tools", (agent_id, allow, deny)))
    monkeypatch.setattr(openclaw, "set_agent_filesystem_binds", lambda agent_id, binds: calls.setdefault("binds", (agent_id, binds)))
    monkeypatch.setattr(openclaw, "set_agent_sandbox", lambda **kw: calls.setdefault("sandbox", kw))
    monkeypatch.setattr(openclaw, "get_agent_config", lambda agent_id: {"id": agent_id})

    resp = client.put(
        "/v1/admin/openclaw/agents/boss/permissions",
        json={"tools_allow": ["read", "write"]},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"id": "boss"}
    assert calls["tools"] == ("boss", ["read", "write"], None)
    assert "binds" not in calls
    assert "sandbox" not in calls


def test_set_agent_permissions_rejects_non_list_tools_allow(client, admin_headers):
    resp = client.put(
        "/v1/admin/openclaw/agents/boss/permissions",
        json={"tools_allow": "read"},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "tools_allow" in resp.get_json()["error_description"]


def test_set_agent_permissions_writes_filesystem_and_sandbox(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(openclaw, "set_agent_filesystem_binds", lambda agent_id, binds: calls.setdefault("binds", binds))
    monkeypatch.setattr(openclaw, "set_agent_sandbox", lambda agent_id, **kw: calls.setdefault("sandbox", (agent_id, kw)))
    monkeypatch.setattr(openclaw, "get_agent_config", lambda agent_id: {"id": agent_id})

    resp = client.put(
        "/v1/admin/openclaw/agents/boss/permissions",
        json={"filesystem_binds": ["/data:/data:ro"], "sandbox_mode": "all", "workspace_access": "rw", "sandbox_network": "none"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert calls["binds"] == ["/data:/data:ro"]
    assert calls["sandbox"] == ("boss", {"mode": "all", "workspace_access": "rw", "network": "none"})


def test_set_agent_permissions_surfaces_cli_error(client, admin_headers, monkeypatch):
    def raise_error(agent_id, allow=None, deny=None):
        raise openclaw.OpenClawCliError("no OpenClaw agent with id 'boss'")

    monkeypatch.setattr(openclaw, "set_agent_tools", raise_error)
    resp = client.put(
        "/v1/admin/openclaw/agents/boss/permissions",
        json={"tools_allow": ["read"]},
        headers=admin_headers,
    )
    assert resp.status_code == 502


def test_list_telegram_bots(client, admin_headers, monkeypatch):
    monkeypatch.setattr(
        openclaw, "list_telegram_bots",
        lambda: [{"id": "default", "name": "default"}, {"id": "support-bot", "name": "Support Bot"}],
    )
    resp = client.get("/v1/admin/openclaw/telegram/bots", headers=admin_headers)
    assert resp.get_json() == {"bots": [{"id": "default", "name": "default"}, {"id": "support-bot", "name": "Support Bot"}]}


def test_list_telegram_bots_surfaces_cli_error(client, admin_headers, monkeypatch):
    def raise_error():
        raise openclaw.OpenClawCliError("gateway not running")

    monkeypatch.setattr(openclaw, "list_telegram_bots", raise_error)
    resp = client.get("/v1/admin/openclaw/telegram/bots", headers=admin_headers)
    assert resp.status_code == 502


def test_add_telegram_bot_requires_name_and_token(client, admin_headers):
    resp = client.post("/v1/admin/openclaw/telegram/bots", json={"token": "tok"}, headers=admin_headers)
    assert resp.status_code == 400
    resp = client.post("/v1/admin/openclaw/telegram/bots", json={"name": "Support Bot"}, headers=admin_headers)
    assert resp.status_code == 400


def test_add_telegram_bot_success(client, admin_headers, monkeypatch):
    calls = {}

    def fake_add(name, token, account_id=None):
        calls["args"] = (name, token, account_id)
        return "support-bot"

    monkeypatch.setattr(openclaw, "add_or_update_telegram_bot", fake_add)
    resp = client.post(
        "/v1/admin/openclaw/telegram/bots", json={"name": "Support Bot", "token": "sk-fake"}, headers=admin_headers
    )
    assert resp.status_code == 201
    assert resp.get_json() == {"id": "support-bot", "name": "Support Bot"}
    assert calls["args"] == ("Support Bot", "sk-fake", None)


def test_add_telegram_bot_passes_explicit_account_id(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        openclaw, "add_or_update_telegram_bot",
        lambda name, token, account_id=None: calls.setdefault("account_id", account_id) or account_id,
    )
    resp = client.post(
        "/v1/admin/openclaw/telegram/bots",
        json={"name": "Support Bot", "token": "sk-fake", "account_id": "support-bot"},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert calls["account_id"] == "support-bot"


def test_add_telegram_bot_surfaces_cli_error(client, admin_headers, monkeypatch):
    def raise_error(name, token, account_id=None):
        raise openclaw.OpenClawCliError("bad token")

    monkeypatch.setattr(openclaw, "add_or_update_telegram_bot", raise_error)
    resp = client.post(
        "/v1/admin/openclaw/telegram/bots", json={"name": "Support Bot", "token": "sk-fake"}, headers=admin_headers
    )
    assert resp.status_code == 502


def test_remove_telegram_bot(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(openclaw, "remove_telegram_bot", lambda account_id: calls.setdefault("account_id", account_id))
    resp = client.delete("/v1/admin/openclaw/telegram/bots/support-bot", headers=admin_headers)
    assert resp.status_code == 204
    assert calls["account_id"] == "support-bot"


def test_remove_telegram_bot_surfaces_cli_error(client, admin_headers, monkeypatch):
    def raise_error(account_id):
        raise openclaw.OpenClawCliError("no such account")

    monkeypatch.setattr(openclaw, "remove_telegram_bot", raise_error)
    resp = client.delete("/v1/admin/openclaw/telegram/bots/support-bot", headers=admin_headers)
    assert resp.status_code == 502


def test_bind_telegram_bot_requires_agent_id(client, admin_headers):
    resp = client.post("/v1/admin/openclaw/telegram/bots/support-bot/bind", json={}, headers=admin_headers)
    assert resp.status_code == 400


def test_bind_telegram_bot_success(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        openclaw, "bind_agent_to_telegram_account",
        lambda agent_id, account_id: calls.setdefault("args", (agent_id, account_id)),
    )
    resp = client.post(
        "/v1/admin/openclaw/telegram/bots/support-bot/bind", json={"agent_id": "boss"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"account_id": "support-bot", "agent_id": "boss"}
    assert calls["args"] == ("boss", "support-bot")


def test_bind_telegram_bot_surfaces_cli_error(client, admin_headers, monkeypatch):
    def raise_error(agent_id, account_id):
        raise openclaw.OpenClawCliError("no OpenClaw agent with id 'boss'")

    monkeypatch.setattr(openclaw, "bind_agent_to_telegram_account", raise_error)
    resp = client.post(
        "/v1/admin/openclaw/telegram/bots/support-bot/bind", json={"agent_id": "boss"}, headers=admin_headers
    )
    assert resp.status_code == 502


def test_get_website_allowlist(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "get_website_allowlist", lambda: ["example.com"])
    resp = client.get("/v1/admin/openclaw/website-allowlist", headers=admin_headers)
    assert resp.get_json() == {"hostnames": ["example.com"]}


def test_set_website_allowlist_requires_hostnames(client, admin_headers):
    resp = client.put("/v1/admin/openclaw/website-allowlist", json={}, headers=admin_headers)
    assert resp.status_code == 400


def test_set_website_allowlist_rejects_non_string_list(client, admin_headers):
    resp = client.put("/v1/admin/openclaw/website-allowlist", json={"hostnames": [1, 2]}, headers=admin_headers)
    assert resp.status_code == 400


def test_set_website_allowlist_writes_and_returns(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(openclaw, "set_website_allowlist", lambda hostnames: calls.setdefault("hostnames", hostnames))
    resp = client.put(
        "/v1/admin/openclaw/website-allowlist", json={"hostnames": ["example.com", "*.trusted.dev"]}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"hostnames": ["example.com", "*.trusted.dev"]}
    assert calls["hostnames"] == ["example.com", "*.trusted.dev"]


def test_tool_catalog_endpoint(client, admin_headers):
    resp = client.get("/v1/admin/openclaw/tool-catalog", headers=admin_headers)
    body = resp.get_json()
    assert "browser" in body["Web access"]


def test_telegram_status_endpoint(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "get_telegram_status", lambda: {"configured": True, "dm_policy": "pairing"})
    resp = client.get("/v1/admin/openclaw/telegram/status", headers=admin_headers)
    assert resp.get_json() == {"configured": True, "dm_policy": "pairing"}


def test_telegram_status_endpoint_surfaces_cli_error(client, admin_headers, monkeypatch):
    def raise_error():
        raise openclaw.OpenClawCliError("gateway not running")

    monkeypatch.setattr(openclaw, "get_telegram_status", raise_error)
    resp = client.get("/v1/admin/openclaw/telegram/status", headers=admin_headers)
    assert resp.status_code == 502


def test_connect_agent_telegram_requires_token(client, admin_headers):
    resp = client.post("/v1/admin/openclaw/agents/boss/telegram", json={}, headers=admin_headers)
    assert resp.status_code == 400


def test_connect_agent_telegram_rejects_invalid_dm_policy(client, admin_headers):
    resp = client.post(
        "/v1/admin/openclaw/agents/boss/telegram", json={"token": "tok", "dm_policy": "whatever"}, headers=admin_headers
    )
    assert resp.status_code == 400


def test_connect_agent_telegram_success(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        openclaw, "connect_telegram_channel",
        lambda token, dm_policy="pairing": calls.setdefault("connect", (token, dm_policy)),
    )
    monkeypatch.setattr(openclaw, "bind_agent_to_telegram", lambda agent_id: calls.setdefault("bind", agent_id))
    resp = client.post(
        "/v1/admin/openclaw/agents/boss/telegram", json={"token": "sk-fake-bot-token"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"channel_connected": True, "agent_bound": True, "dm_policy": "pairing"}
    assert calls["connect"] == ("sk-fake-bot-token", "pairing")
    assert calls["bind"] == "boss"


def test_connect_agent_telegram_channel_failure_does_not_attempt_bind(client, admin_headers, monkeypatch):
    def raise_error(token, dm_policy="pairing"):
        raise openclaw.OpenClawCliError("bad token")

    monkeypatch.setattr(openclaw, "connect_telegram_channel", raise_error)

    def fail_if_called(agent_id):
        raise AssertionError("must not attempt bind if the channel never connected")

    monkeypatch.setattr(openclaw, "bind_agent_to_telegram", fail_if_called)
    resp = client.post(
        "/v1/admin/openclaw/agents/boss/telegram", json={"token": "sk-fake-bot-token"}, headers=admin_headers
    )
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["channel_connected"] is False
    assert body["agent_bound"] is False
    assert body["error"] == "bad token"


def test_connect_agent_telegram_bind_failure_after_channel_succeeds(client, admin_headers, monkeypatch):
    monkeypatch.setattr(openclaw, "connect_telegram_channel", lambda token, dm_policy="pairing": None)

    def raise_error(agent_id):
        raise openclaw.OpenClawCliError("no OpenClaw agent with id 'boss'")

    monkeypatch.setattr(openclaw, "bind_agent_to_telegram", raise_error)
    resp = client.post(
        "/v1/admin/openclaw/agents/boss/telegram", json={"token": "sk-fake-bot-token"}, headers=admin_headers
    )
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["channel_connected"] is True
    assert body["agent_bound"] is False


def test_connect_agent_telegram_open_policy(client, admin_headers, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        openclaw, "connect_telegram_channel",
        lambda token, dm_policy="pairing": calls.setdefault("dm_policy", dm_policy),
    )
    monkeypatch.setattr(openclaw, "bind_agent_to_telegram", lambda agent_id: None)
    resp = client.post(
        "/v1/admin/openclaw/agents/boss/telegram", json={"token": "tok", "dm_policy": "open"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert calls["dm_policy"] == "open"
    assert resp.get_json()["dm_policy"] == "open"
