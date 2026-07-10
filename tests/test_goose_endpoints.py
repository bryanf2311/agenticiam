import json
import sys

import pytest
import yaml

from agenticiam import api as api_module
from agenticiam import db as db_module, goose, tokens
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


@pytest.fixture
def goose_config_path(tmp_path, monkeypatch):
    path = tmp_path / "goose-config.yaml"
    monkeypatch.setattr(goose, "config_path", lambda: path)
    return path


def test_goose_status_reports_tool_presence(client, admin_headers, monkeypatch, goose_config_path):
    monkeypatch.setattr(goose, "find_goose_binary", lambda: "/usr/bin/goose")
    monkeypatch.setattr(goose, "find_ollama_binary", lambda: None)
    monkeypatch.setattr(goose, "list_ollama_models", lambda: (_ for _ in ()).throw(goose.OllamaUnavailable("no")))

    resp = client.get("/v1/admin/goose/status", headers=admin_headers)
    body = resp.get_json()
    assert body["goose_installed"] is True
    assert body["goose_path"] == "/usr/bin/goose"
    assert body["ollama_installed"] is False
    assert body["ollama_reachable"] is False
    assert body["config_path"] == str(goose_config_path)


def test_goose_status_requires_admin(client, directory):
    identity = directory.create_identity("agent", "bot1")
    token = tokens.issue_access_token(identity, [])
    resp = client.get("/v1/admin/goose/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_goose_models_available_defaults_to_ollama(client, admin_headers, monkeypatch):
    monkeypatch.setattr(goose, "list_ollama_models", lambda timeout=2.0: ["llama3.1:8b", "phi4:latest"])
    resp = client.post("/v1/admin/goose/models", json={}, headers=admin_headers)
    body = resp.get_json()
    assert body == {"available": True, "models": ["llama3.1:8b", "phi4:latest"]}


def test_goose_models_unavailable(client, admin_headers, monkeypatch):
    def raise_unavailable(timeout=2.0):
        raise goose.OllamaUnavailable("connection refused")

    monkeypatch.setattr(goose, "list_ollama_models", raise_unavailable)
    resp = client.post("/v1/admin/goose/models", json={"provider": "ollama"}, headers=admin_headers)
    body = resp.get_json()
    assert body["available"] is False
    assert "connection refused" in body["error"]


def test_goose_models_anthropic_requires_api_key(client, admin_headers, monkeypatch):
    resp = client.post("/v1/admin/goose/models", json={"provider": "anthropic"}, headers=admin_headers)
    body = resp.get_json()
    assert body["available"] is False
    assert "API key" in body["error"]


def test_goose_models_anthropic_with_key(client, admin_headers, monkeypatch):
    monkeypatch.setattr(
        goose, "list_anthropic_models", lambda api_key, timeout=5.0: ["claude-opus-4-8", "claude-sonnet-5"]
    )
    resp = client.post(
        "/v1/admin/goose/models", json={"provider": "anthropic", "api_key": "sk-ant-fake"}, headers=admin_headers
    )
    body = resp.get_json()
    assert body == {"available": True, "models": ["claude-opus-4-8", "claude-sonnet-5"]}


def test_goose_models_google_with_key(client, admin_headers, monkeypatch):
    monkeypatch.setattr(goose, "list_google_models", lambda api_key, timeout=5.0: ["gemini-3-pro"])
    resp = client.post(
        "/v1/admin/goose/models", json={"provider": "google", "api_key": "fake-key"}, headers=admin_headers
    )
    body = resp.get_json()
    assert body == {"available": True, "models": ["gemini-3-pro"]}


def test_goose_models_unknown_provider(client, admin_headers):
    resp = client.post("/v1/admin/goose/models", json={"provider": "not-a-real-provider"}, headers=admin_headers)
    assert resp.status_code == 400


def test_create_goose_agent_end_to_end(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={
            "name": "research-bot",
            "permissions": ["files:read", "shell:exec"],
            "model": "llama3.1:8b",
            "cmd": "agenticiam",
            "args": ["mcp"],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["identity"]["name"] == "research-bot"
    assert body["identity"]["kind"] == "agent"
    assert body["goose_config_written"] is True
    commands = body["launch_commands"]
    assert commands["bash"] == "GOOSE_PROVIDER=ollama GOOSE_MODEL=llama3.1:8b goose session -n research-bot"
    assert "$env:GOOSE_MODEL=\"llama3.1:8b\"" in commands["powershell"]
    assert "set \"GOOSE_MODEL=llama3.1:8b\"" in commands["cmd"]
    # goose session has no --provider/--model flags — must never appear in any variant
    for variant in commands.values():
        assert "--provider" not in variant
        assert "--model" not in variant

    written = goose.load_config()
    entry = written["extensions"]["research-bot"]
    assert entry["cmd"] == "agenticiam"
    assert entry["args"] == ["mcp"]
    assert entry["envs"]["AGENTICIAM_TOKEN"].startswith("aiam_")
    assert entry["enabled"] is True
    # set_as_default was not requested, so global provider/model must be untouched
    assert "GOOSE_PROVIDER" not in written

    # the identity really has the requested permissions
    perms = client.get("/v1/admin/identities/research-bot/permissions", headers=admin_headers).get_json()
    assert set(perms) == {"files:read", "shell:exec"}


def test_create_goose_agent_set_as_default(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={"name": "bot2", "permissions": [], "model": "llama3.1:8b", "set_as_default": True},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    written = goose.load_config()
    assert written["GOOSE_PROVIDER"] == "ollama"
    assert written["GOOSE_MODEL"] == "llama3.1:8b"
    # the model is already the global default now, so no env-var override needed
    commands = resp.get_json()["launch_commands"]
    assert commands["bash"] == "goose session -n bot2"


def test_create_goose_agent_with_anthropic_key_never_persisted(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={
            "name": "claude-bot",
            "permissions": [],
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "api_key": "sk-ant-super-secret",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()
    # the key must appear ONLY in the one-time launch command response...
    assert "sk-ant-super-secret" in body["launch_commands"]["bash"]

    # ...and nowhere else: not in goose's config.yaml...
    written = goose.load_config()
    assert "sk-ant-super-secret" not in yaml.safe_dump(written)

    # ...and not anywhere in AgenticIAM's own directory/audit trail
    audit_entries = client.get("/v1/admin/audit?limit=50", headers=admin_headers).get_json()
    assert not any("sk-ant-super-secret" in json.dumps(e) for e in audit_entries)
    identities = client.get("/v1/admin/identities", headers=admin_headers).get_json()
    assert not any("sk-ant-super-secret" in json.dumps(i) for i in identities)


def test_create_goose_agent_context_limit_set_as_default(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={
            "name": "bot5",
            "permissions": [],
            "model": "llama3.1:8b",
            "context_limit": 32000,
            "set_as_default": True,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    written = goose.load_config()
    assert written["GOOSE_CONTEXT_LIMIT"] == 32000
    assert written["GOOSE_INPUT_LIMIT"] == 32000
    commands = resp.get_json()["launch_commands"]
    assert commands["bash"] == "goose session -n bot5"  # already the default, no override needed


def test_create_goose_agent_context_limit_not_default_uses_env_override(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={"name": "bot6", "permissions": [], "model": "llama3.1:8b", "context_limit": 32000},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    written = goose.load_config()
    assert "GOOSE_CONTEXT_LIMIT" not in written
    commands = resp.get_json()["launch_commands"]
    assert "GOOSE_CONTEXT_LIMIT=32000" in commands["bash"]
    assert "GOOSE_INPUT_LIMIT=32000" in commands["bash"]


def test_create_goose_agent_invalid_context_limit(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={"name": "bot7", "permissions": [], "context_limit": "not-a-number"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


def test_create_goose_agent_preserves_existing_config(client, admin_headers, goose_config_path):
    goose.save_config({"GOOSE_PROVIDER": "anthropic", "GOOSE_MODEL": "claude", "extensions": {"other": {"name": "Other"}}})
    resp = client.post(
        "/v1/admin/goose/agents", json={"name": "bot3", "permissions": []}, headers=admin_headers
    )
    assert resp.status_code == 201
    written = goose.load_config()
    assert written["GOOSE_PROVIDER"] == "anthropic"  # untouched, set_as_default wasn't passed
    assert written["extensions"]["other"] == {"name": "Other"}
    assert "bot3" in written["extensions"]


def test_create_goose_agent_missing_name(client, admin_headers):
    resp = client.post("/v1/admin/goose/agents", json={"permissions": []}, headers=admin_headers)
    assert resp.status_code == 400


def test_create_goose_agent_requires_admin(client, directory):
    identity = directory.create_identity("agent", "bot1")
    token = tokens.issue_access_token(identity, [])
    resp = client.post(
        "/v1/admin/goose/agents", json={"name": "x", "permissions": []}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_create_goose_agent_survives_config_write_failure(client, admin_headers, goose_config_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(goose, "save_config", fail)
    resp = client.post(
        "/v1/admin/goose/agents",
        json={"name": "bot4", "permissions": ["files:read"]},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["goose_config_written"] is False
    assert "permission denied" in body["goose_config_error"]
    assert "AGENTICIAM_TOKEN" in body["manual_extension_snippet"]

    # the AgenticIAM identity must still exist despite the config write failure
    listing = client.get("/v1/admin/identities", headers=admin_headers).get_json()
    assert "bot4" in [i["name"] for i in listing]


def test_create_goose_agent_duplicate_name_conflicts(client, admin_headers, goose_config_path):
    client.post("/v1/admin/goose/agents", json={"name": "dup", "permissions": []}, headers=admin_headers)
    resp = client.post("/v1/admin/goose/agents", json={"name": "dup", "permissions": []}, headers=admin_headers)
    assert resp.status_code == 409


def test_self_command_not_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    cmd, args = api_module._self_command()
    assert cmd == sys.executable
    assert args == ["-m", "agenticiam", "mcp"]


def test_self_command_frozen_cli_binary(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/agenticiam/agenticiam")
    cmd, args = api_module._self_command()
    assert cmd == "/opt/agenticiam/agenticiam"
    assert args == ["mcp"]


def test_self_command_frozen_gui_binary(monkeypatch):
    # the GUI exe understands "mcp" as an argument too (see gui.main), so
    # whichever binary is actually running the server is the right answer —
    # no more guessing whether a separate CLI binary exists alongside it
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/agenticiam/agenticiam-gui")
    cmd, args = api_module._self_command()
    assert cmd == "/opt/agenticiam/agenticiam-gui"
    assert args == ["mcp"]
