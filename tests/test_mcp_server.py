import json

import pytest

from agenticiam import mcp_server


def _principal(identity, scopes):
    return {
        "active": True,
        "sub": identity["id"],
        "name": identity["name"],
        "kind": identity["kind"],
        "scopes": scopes,
    }


def test_whoami_requires_auth(directory):
    with pytest.raises(PermissionError):
        mcp_server.h_whoami(directory, None, {})


def test_whoami_returns_principal(directory):
    identity = directory.create_identity("agent", "bot1")
    principal = _principal(identity, ["shell:exec"])
    assert mcp_server.h_whoami(directory, principal, {})["name"] == "bot1"


def test_check_permission(directory):
    identity = directory.create_identity("agent", "bot1")
    principal = _principal(identity, ["shell:exec"])
    assert mcp_server.h_check_permission(directory, principal, {"action": "shell:exec"})["allowed"] is True
    assert mcp_server.h_check_permission(directory, principal, {"action": "email:send"})["allowed"] is False


def test_admin_tool_requires_admin_scope(directory):
    identity = directory.create_identity("agent", "bot1")
    principal = _principal(identity, ["shell:exec"])
    with pytest.raises(PermissionError):
        mcp_server.h_create_identity(directory, principal, {"kind": "user", "name": "x"})


def test_admin_tool_succeeds_with_admin_scope(directory):
    identity = directory.create_identity("agent", "bot1")
    principal = _principal(identity, ["iam:admin"])
    result = mcp_server.h_create_identity(directory, principal, {"kind": "user", "name": "x"})
    assert result["name"] == "x"


def test_effective_permissions_self_vs_other(directory):
    identity = directory.create_identity("agent", "bot1")
    directory.create_identity("user", "alice")
    principal = _principal(identity, [])
    assert mcp_server.h_effective_permissions(directory, principal, {}) == []
    with pytest.raises(PermissionError):
        mcp_server.h_effective_permissions(directory, principal, {"identity": "alice"})


def test_tools_list_matches_handlers():
    schema_names = {t["name"] for t in mcp_server.TOOL_SCHEMAS}
    handler_names = set(mcp_server.TOOL_HANDLERS.keys())
    assert schema_names == handler_names


def test_json_rpc_message_handling(directory, monkeypatch):
    outputs = []
    monkeypatch.setattr(mcp_server, "_send", outputs.append)

    mcp_server._handle_message(directory, None, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert outputs[0]["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION

    mcp_server._handle_message(directory, None, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(outputs[1]["result"]["tools"]) == len(mcp_server.TOOL_SCHEMAS)

    mcp_server._handle_message(
        directory, None,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "iam_whoami", "arguments": {}}},
    )
    assert outputs[2]["result"]["isError"] is True

    mcp_server._handle_message(directory, None, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert len(outputs) == 3  # notifications get no response

    mcp_server._handle_message(directory, None, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                   "params": {"name": "does_not_exist", "arguments": {}}})
    assert outputs[3]["error"]["code"] == -32601


def test_serve_stdio_handshake_never_touches_disk(monkeypatch):
    """initialize / tools/list / ping / notifications must be answerable
    with zero I/O — this is what lets a strict stdio client's handshake
    timeout (e.g. Goose) succeed even before AGENTICIAM_HOME is writable."""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("db.connect should not be called before the first tools/call")

    monkeypatch.setattr(mcp_server.db, "connect", fail_if_called)
    sent = []
    monkeypatch.setattr(mcp_server, "_send", sent.append)

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
    ]
    mcp_server.serve_stdio(in_stream=iter(lines))

    assert sent[0]["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert len(sent[1]["result"]["tools"]) == len(mcp_server.TOOL_SCHEMAS)
    assert sent[2]["result"] == {}


def test_dispatch_requires_permission(directory):
    directory.create_identity(
        "agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}}
    )
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, [])  # no dispatch:* grant
    with pytest.raises(PermissionError):
        mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "hi"})


def test_dispatch_non_goose_agent_raises_value_error(directory):
    directory.create_identity("agent", "plain-agent")
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:plain-agent"])
    with pytest.raises(ValueError, match="not created as a Goose agent"):
        mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "plain-agent", "task": "hi"})


def test_dispatch_non_ollama_provider_raises_value_error(directory):
    directory.create_identity(
        "agent", "cloud-worker", metadata={"goose": {"provider": "anthropic", "model": "claude-sonnet-5"}}
    )
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:cloud-worker"])
    with pytest.raises(ValueError, match="only supports Ollama"):
        mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "cloud-worker", "task": "hi"})


def test_dispatch_success(directory, monkeypatch):
    monkeypatch.setattr(mcp_server.goose, "run_agent_task", lambda provider, model, task, **kw: f"did: {task}")
    directory.create_identity(
        "agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}}
    )
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:worker1"])
    result = mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "summarize"})
    assert result == {"agent": "worker1", "response": "did: summarize"}


def test_dispatch_wildcard_permission_works(directory, monkeypatch):
    monkeypatch.setattr(mcp_server.goose, "run_agent_task", lambda provider, model, task, **kw: "ok")
    directory.create_identity(
        "agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}}
    )
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:*"])
    result = mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "hi"})
    assert result["response"] == "ok"


def test_dispatch_passes_custom_timeout(directory, monkeypatch):
    captured = {}

    def fake_run(provider, model, task, timeout=None):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(mcp_server.goose, "run_agent_task", fake_run)
    directory.create_identity("agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}})
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:worker1"])
    mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "hi", "timeout_seconds": 600})
    assert captured["timeout"] == 600


def test_dispatch_defaults_to_300s_timeout(directory, monkeypatch):
    captured = {}

    def fake_run(provider, model, task, timeout=None):
        captured["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(mcp_server.goose, "run_agent_task", fake_run)
    directory.create_identity("agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}})
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:worker1"])
    mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "hi"})
    assert captured["timeout"] == mcp_server.goose.DEFAULT_DISPATCH_TIMEOUT


def test_dispatch_writes_started_and_success_audit_entries(directory, monkeypatch):
    monkeypatch.setattr(mcp_server.goose, "run_agent_task", lambda provider, model, task, **kw: "ok")
    directory.create_identity("agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}})
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:worker1"])
    mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "hi"})

    entries = mcp_server.audit.tail(directory.conn, limit=10)
    actions_and_results = [(e["action"], e["result"]) for e in entries]
    assert ("dispatch.worker1", "started") in actions_and_results
    assert ("dispatch.worker1", "success") in actions_and_results


def test_dispatch_writes_failure_audit_entry_on_error(directory, monkeypatch):
    def fail(provider, model, task, **kw):
        raise mcp_server.goose.DispatchError("boom")

    monkeypatch.setattr(mcp_server.goose, "run_agent_task", fail)
    directory.create_identity("agent", "worker1", metadata={"goose": {"provider": "ollama", "model": "llama3.1:8b"}})
    manager = directory.create_identity("agent", "manager1")
    principal = _principal(manager, ["dispatch:worker1"])
    with pytest.raises(mcp_server.goose.DispatchError):
        mcp_server.h_dispatch_to_agent(directory, principal, {"agent": "worker1", "task": "hi"})

    entries = mcp_server.audit.tail(directory.conn, limit=10)
    actions_and_results = [(e["action"], e["result"]) for e in entries]
    assert ("dispatch.worker1", "started") in actions_and_results
    assert ("dispatch.worker1", "failure") in actions_and_results


def test_serve_stdio_initializes_directory_lazily_on_first_tools_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICIAM_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(mcp_server, "_send", sent.append)

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "iam_whoami", "arguments": {}}}
        ),
    ]
    mcp_server.serve_stdio(db_path=tmp_path / "test.db", in_stream=iter(lines))

    assert sent[0]["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    # no AGENTICIAM_TOKEN set, so the lazily-created principal is None —
    # the tool call should fail cleanly (isError) rather than crash the loop
    assert sent[1]["result"]["isError"] is True
    assert (tmp_path / "test.db").exists()
