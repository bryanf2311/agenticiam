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


# ---------------------------------------------------------------- dispatch (manager -> worker)

@pytest.fixture
def ollama_worker(directory):
    return directory.create_identity(
        "agent", "worker1", display_name="worker1",
        metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b", "context_limit": None}},
    )


@pytest.fixture
def manager_token(directory, ollama_worker):
    directory.create_role("manager-role")
    directory.grant_permission("manager-role", f"dispatch:{ollama_worker['name']}")
    manager = directory.create_identity("agent", "manager1")
    directory.assign_role("manager-role", "identity", "manager1")
    return tokens.issue_access_token(manager, [f"dispatch:{ollama_worker['name']}"])


def test_dispatch_requires_auth(client, ollama_worker):
    resp = client.post(f"/v1/agents/{ollama_worker['name']}/dispatch", json={"task": "hi"})
    assert resp.status_code == 401


def test_dispatch_requires_dispatch_permission(client, directory, ollama_worker):
    identity = directory.create_identity("agent", "no-perms")
    token = tokens.issue_access_token(identity, [])
    resp = client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_dispatch_unknown_agent(client, directory, ollama_worker):
    # dispatch:* so the permission check passes and we actually reach the
    # existence check (a scoped-but-wrong-name token correctly 403s first —
    # no information-disclosure oracle for callers without the right grant)
    directory.create_role("wildcard-role")
    directory.grant_permission("wildcard-role", "dispatch:*")
    manager = directory.create_identity("agent", "wildcard-manager")
    directory.assign_role("wildcard-role", "identity", "wildcard-manager")
    token = tokens.issue_access_token(manager, ["dispatch:*"])
    resp = client.post(
        "/v1/agents/does-not-exist/dispatch", json={"task": "hi"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


def test_dispatch_non_goose_agent_rejected(client, directory, manager_token):
    directory.create_identity("agent", "plain-agent")
    token = tokens.issue_access_token(directory.get_identity("manager1"), ["dispatch:plain-agent"])
    resp = client.post(
        "/v1/agents/plain-agent/dispatch", json={"task": "hi"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 400
    assert "not created as a Goose agent" in resp.get_json()["error_description"]


def test_dispatch_non_ollama_provider_rejected(client, directory, manager_token):
    directory.create_identity(
        "agent", "cloud-worker", metadata={"goose": {"provider": "anthropic", "model": "claude-sonnet-5"}}
    )
    token = tokens.issue_access_token(directory.get_identity("manager1"), ["dispatch:cloud-worker"])
    resp = client.post(
        "/v1/agents/cloud-worker/dispatch", json={"task": "hi"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 400
    assert "only supports Ollama" in resp.get_json()["error_description"]


def test_dispatch_missing_task(client, manager_token, ollama_worker):
    resp = client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch", json={}, headers={"Authorization": f"Bearer {manager_token}"}
    )
    assert resp.status_code == 400


def test_dispatch_success(client, manager_token, ollama_worker, monkeypatch):
    monkeypatch.setattr(goose, "run_agent_task", lambda provider, model, task, **kw: f"[{provider}/{model}] {task}")
    resp = client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "summarize the README"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"agent": "worker1", "response": "[ollama/llama3.1:8b] summarize the README"}


def test_dispatch_writes_started_audit_entry_with_timeout(client, manager_token, ollama_worker, monkeypatch, admin_headers):
    monkeypatch.setattr(goose, "run_agent_task", lambda provider, model, task, **kw: "ok")
    client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "hi", "timeout_seconds": 600},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    entries = client.get("/v1/admin/audit?limit=10", headers=admin_headers).get_json()
    started = next(e for e in entries if e["action"] == "dispatch.worker1" and e["result"] == "started")
    assert json.loads(started["detail"])["timeout_seconds"] == 600


def test_dispatch_passes_custom_timeout_to_run_agent_task(client, manager_token, ollama_worker, monkeypatch):
    captured = {}

    def fake_run(provider, model, task, timeout=None):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(goose, "run_agent_task", fake_run)
    client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "hi", "timeout_seconds": 600},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert captured["timeout"] == 600


def test_dispatch_defaults_to_300s(client, manager_token, ollama_worker, monkeypatch):
    captured = {}

    def fake_run(provider, model, task, timeout=None):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(goose, "run_agent_task", fake_run)
    client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "hi"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert captured["timeout"] == goose.DEFAULT_DISPATCH_TIMEOUT


def test_dispatch_failure_returns_502(client, manager_token, ollama_worker, monkeypatch):
    def fail(provider, model, task, **kw):
        raise goose.DispatchError("goose executable not found")

    monkeypatch.setattr(goose, "run_agent_task", fail)
    resp = client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "hi"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert resp.status_code == 502
    assert "goose executable not found" in resp.get_json()["error_description"]


def test_dispatch_wildcard_permission(client, directory, ollama_worker, monkeypatch):
    monkeypatch.setattr(goose, "run_agent_task", lambda provider, model, task, **kw: "ok")
    directory.create_role("super-manager-role")
    directory.grant_permission("super-manager-role", "dispatch:*")
    manager = directory.create_identity("agent", "super-manager")
    directory.assign_role("super-manager-role", "identity", "super-manager")
    token = tokens.issue_access_token(manager, ["dispatch:*"])
    resp = client.post(
        f"/v1/agents/{ollama_worker['name']}/dispatch",
        json={"task": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["response"] == "ok"


# ---------------------------------------------------------------- groups (team workflow)

def test_create_goose_agent_with_new_group(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents",
        json={"name": "marketing-boss", "permissions": [], "group": "marketing"},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.get_json()["group"] == "marketing"

    members = client.get("/v1/admin/groups/marketing/members", headers=admin_headers).get_json()
    assert [m["name"] for m in members] == ["marketing-boss"]


def test_create_goose_agent_with_existing_group(client, admin_headers, directory, goose_config_path):
    directory.create_group("marketing", "The marketing team")
    resp = client.post(
        "/v1/admin/goose/agents",
        json={"name": "marketing-intern", "permissions": [], "group": "marketing"},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    members = client.get("/v1/admin/groups/marketing/members", headers=admin_headers).get_json()
    assert [m["name"] for m in members] == ["marketing-intern"]


def test_create_goose_agent_without_group_field_unaffected(client, admin_headers, goose_config_path):
    resp = client.post(
        "/v1/admin/goose/agents", json={"name": "lone-agent", "permissions": []}, headers=admin_headers
    )
    assert resp.status_code == 201
    assert resp.get_json()["group"] is None


def test_group_launch_commands(client, admin_headers, goose_config_path):
    client.post(
        "/v1/admin/goose/agents",
        json={"name": "marketing-boss", "permissions": [], "provider": "ollama", "model": "llama3.1:8b", "group": "marketing"},
        headers=admin_headers,
    )
    client.post(
        "/v1/admin/goose/agents",
        json={"name": "marketing-intern", "permissions": [], "provider": "ollama", "model": "phi4", "group": "marketing"},
        headers=admin_headers,
    )
    resp = client.get("/v1/admin/groups/marketing/launch-commands", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["group"] == "marketing"
    names = {a["name"] for a in body["agents"]}
    assert names == {"marketing-boss", "marketing-intern"}
    boss = next(a for a in body["agents"] if a["name"] == "marketing-boss")
    assert "goose session -n marketing-boss" in boss["launch_commands"]["bash"]
    assert "GOOSE_MODEL=llama3.1:8b" in boss["launch_commands"]["bash"]


def test_group_launch_commands_skips_non_goose_members(client, admin_headers, directory, goose_config_path):
    directory.create_group("marketing")
    directory.create_identity("agent", "plain-member")
    directory.add_member("marketing", "plain-member")
    resp = client.get("/v1/admin/groups/marketing/launch-commands", headers=admin_headers)
    assert resp.status_code == 200
    agent = resp.get_json()["agents"][0]
    assert agent["name"] == "plain-member"
    assert agent["launch_commands"] is None


def test_group_launch_commands_unknown_group(client, admin_headers):
    resp = client.get("/v1/admin/groups/does-not-exist/launch-commands", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------- delete cleans up config.yaml

def test_delete_non_goose_identity_returns_204(client, admin_headers, directory):
    directory.create_identity("agent", "plain-agent")
    resp = client.delete("/v1/admin/identities/plain-agent", headers=admin_headers)
    assert resp.status_code == 204
    assert resp.data == b""


def test_delete_goose_identity_removes_extension_from_config(client, admin_headers, goose_config_path):
    client.post(
        "/v1/admin/goose/agents",
        json={"name": "marketing-boss", "permissions": [], "provider": "ollama", "model": "llama3.1:8b"},
        headers=admin_headers,
    )
    assert "marketing-boss" in goose.load_config()["extensions"]

    resp = client.delete("/v1/admin/identities/marketing-boss", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["goose_config_updated"] is True
    assert "marketing-boss" not in goose.load_config().get("extensions", {})

    # identity is really gone
    listing = client.get("/v1/admin/identities", headers=admin_headers).get_json()
    assert "marketing-boss" not in [i["name"] for i in listing]


def test_delete_goose_identity_preserves_other_extensions(client, admin_headers, goose_config_path):
    client.post(
        "/v1/admin/goose/agents", json={"name": "keep-me", "permissions": [], "provider": "ollama", "model": "llama3.1:8b"},
        headers=admin_headers,
    )
    client.post(
        "/v1/admin/goose/agents", json={"name": "delete-me", "permissions": [], "provider": "ollama", "model": "llama3.1:8b"},
        headers=admin_headers,
    )
    client.delete("/v1/admin/identities/delete-me", headers=admin_headers)
    remaining = goose.load_config()["extensions"]
    assert "keep-me" in remaining
    assert "delete-me" not in remaining


def test_delete_goose_identity_config_write_failure_gives_manual_instructions(
    client, admin_headers, goose_config_path, monkeypatch
):
    client.post(
        "/v1/admin/goose/agents",
        json={"name": "marketing-boss", "permissions": [], "provider": "ollama", "model": "llama3.1:8b"},
        headers=admin_headers,
    )
    monkeypatch.setattr(goose, "save_config", lambda cfg: (_ for _ in ()).throw(OSError("permission denied")))
    resp = client.delete("/v1/admin/identities/marketing-boss", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["goose_config_updated"] is False
    assert "permission denied" in body["goose_config_error"]
    assert "marketing-boss" in body["manual_removal_instructions"]
    # the identity itself is still gone even though the config write failed
    listing = client.get("/v1/admin/identities", headers=admin_headers).get_json()
    assert "marketing-boss" not in [i["name"] for i in listing]


def test_delete_goose_identity_missing_from_config_noted_not_errored(client, admin_headers, directory, goose_config_path):
    directory.create_identity(
        "agent", "orphan-meta", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}}
    )
    resp = client.delete("/v1/admin/identities/orphan-meta", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["goose_config_updated"] is False
    assert "goose_config_note" in body
