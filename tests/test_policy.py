from agenticiam.policy import is_allowed, permission_matches


def test_exact_match():
    assert permission_matches("shell:exec", "shell:exec")
    assert not permission_matches("shell:exec", "shell:read")


def test_global_wildcard():
    assert permission_matches("*", "anything:at:all")


def test_prefix_wildcard():
    assert permission_matches("files:*", "files:read")
    assert permission_matches("files:*", "files:write")
    assert not permission_matches("files:*", "shell:exec")
    assert not permission_matches("file:*", "files:read")  # no partial-segment match


def test_is_allowed_across_multiple_grants():
    granted = {"files:*", "iam:admin"}
    assert is_allowed(granted, "files:write")
    assert is_allowed(granted, "iam:admin")
    assert not is_allowed(granted, "shell:exec")
    assert not is_allowed(set(), "files:read")
