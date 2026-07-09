import time

import pytest

from agenticiam import tokens


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICIAM_HOME", str(tmp_path))


def test_encode_decode_roundtrip():
    token = tokens.encode({"sub": "abc", "exp": time.time() + 60})
    payload = tokens.decode(token)
    assert payload["sub"] == "abc"


def test_tampered_token_rejected():
    token = tokens.encode({"sub": "abc", "exp": time.time() + 60})
    header, payload, sig = token.split(".")
    tampered = f"{header}.{payload}.{sig[:-2]}xx"
    with pytest.raises(tokens.TokenError):
        tokens.decode(tampered)


def test_expired_token_rejected():
    token = tokens.encode({"sub": "abc", "exp": time.time() - 10})
    with pytest.raises(tokens.TokenError):
        tokens.decode(token)


def test_signing_key_persists_across_calls(tmp_path):
    key1 = tokens.load_signing_key()
    key2 = tokens.load_signing_key()
    assert key1 == key2


def test_issue_and_introspect_access_token(directory):
    identity = directory.create_identity("agent", "bot1")
    directory.create_role("r")
    directory.grant_permission("r", "shell:exec")
    directory.assign_role("r", "identity", "bot1")

    token = tokens.issue_access_token(identity, ["shell:exec"])
    info = tokens.introspect(token, directory)
    assert info["active"] is True
    assert info["name"] == "bot1"
    assert info["scopes"] == ["shell:exec"]


def test_introspect_rejects_disabled_identity(directory):
    identity = directory.create_identity("agent", "bot1")
    token = tokens.issue_access_token(identity, ["shell:exec"])
    directory.set_enabled("bot1", False)
    info = tokens.introspect(token, directory)
    assert info["active"] is False


def test_introspect_garbage_token(directory):
    assert tokens.introspect("not-a-real-token", directory)["active"] is False


def test_api_key_introspection(directory):
    directory.create_identity("agent", "bot1")
    key = directory.create_api_key("bot1", scopes=["files:read"])
    info = tokens.introspect(key["key"], directory)
    assert info["active"] is True
    assert info["scopes"] == ["files:read"]
