import pytest

from agenticiam.directory import ConflictError, NotFoundError


def test_create_and_get_identity(directory):
    identity = directory.create_identity("user", "alice", secret="hunter2ish")
    assert identity["kind"] == "user"
    assert identity["name"] == "alice"
    assert "secret_hash" not in identity
    fetched = directory.get_identity("alice")
    assert fetched["id"] == identity["id"]


def test_create_agent_generates_secret(directory):
    identity = directory.create_identity("agent", "bot1")
    assert "secret" in identity
    assert len(identity["secret"]) > 10
    # secret is not persisted in plaintext, and not returned on re-fetch
    refetched = directory.get_identity("bot1")
    assert "secret" not in refetched


def test_duplicate_identity_conflicts(directory):
    directory.create_identity("user", "alice")
    with pytest.raises(ConflictError):
        directory.create_identity("user", "alice")


def test_get_missing_identity_raises(directory):
    with pytest.raises(NotFoundError):
        directory.get_identity("nobody")


def test_authenticate(directory):
    directory.create_identity("user", "alice", secret="correct-horse")
    assert directory.authenticate("alice", "correct-horse") is not None
    assert directory.authenticate("alice", "wrong") is None
    assert directory.authenticate("nobody", "wrong") is None


def test_disabled_identity_cannot_authenticate(directory):
    directory.create_identity("user", "alice", secret="pw")
    directory.set_enabled("alice", False)
    assert directory.authenticate("alice", "pw") is None


def test_groups_and_membership(directory):
    directory.create_identity("user", "alice")
    directory.create_group("engineering")
    directory.add_member("engineering", "alice")
    members = directory.list_members("engineering")
    assert [m["name"] for m in members] == ["alice"]
    groups = directory.list_identity_groups("alice")
    assert [g["name"] for g in groups] == ["engineering"]

    directory.remove_member("engineering", "alice")
    assert directory.list_members("engineering") == []


def test_effective_permissions_via_direct_role_and_group_role(directory):
    directory.create_identity("agent", "bot1")
    directory.create_identity("user", "alice")
    directory.create_group("engineering")
    directory.add_member("engineering", "alice")

    directory.create_role("bot-role")
    directory.grant_permission("bot-role", "shell:exec")
    directory.assign_role("bot-role", "identity", "bot1")

    directory.create_role("eng-role")
    directory.grant_permission("eng-role", "files:*")
    directory.assign_role("eng-role", "group", "engineering")

    assert directory.effective_permissions("bot1") == {"shell:exec"}
    assert directory.effective_permissions("alice") == {"files:*"}


def test_effective_permissions_empty_for_unassigned_identity(directory):
    directory.create_identity("user", "nobody-role")
    assert directory.effective_permissions("nobody-role") == set()


def test_api_key_lifecycle(directory):
    directory.create_identity("agent", "bot1")
    key = directory.create_api_key("bot1", scopes=["files:read"])
    result = directory.verify_api_key(key["key"])
    assert result is not None
    assert result["identity"]["name"] == "bot1"
    assert result["scopes"] == ["files:read"]

    directory.revoke_api_key(key["id"])
    assert directory.verify_api_key(key["key"]) is None


def test_api_key_expiry(directory):
    directory.create_identity("agent", "bot1")
    key = directory.create_api_key("bot1", ttl_seconds=-10)  # already expired
    assert directory.verify_api_key(key["key"]) is None


def test_delete_identity_cascades_group_membership(directory):
    directory.create_identity("user", "alice")
    directory.create_group("engineering")
    directory.add_member("engineering", "alice")
    directory.delete_identity("alice")
    assert directory.list_members("engineering") == []
