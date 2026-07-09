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
