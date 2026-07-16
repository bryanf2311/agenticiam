"""RBAC permission matching.

Permission strings are namespaced like "files:read", "shell:exec",
"browser:control", "email:send", "mcp:tool:*", "iam:admin". A role can grant
an exact permission, a prefix wildcard ("files:*"), or the global wildcard
("*"). This is deliberately simple (no resource-instance ABAC) so it stays
auditable at a glance, the same way AD group-based access is easy to reason
about even though it isn't as expressive as a full policy language.
"""


def permission_matches(granted: str, requested: str) -> bool:
    if granted == "*":
        return True
    if granted == requested:
        return True
    if granted.endswith(":*"):
        prefix = granted[:-1]  # keep trailing ':'
        return requested.startswith(prefix)
    return False


def is_allowed(granted_permissions, requested: str) -> bool:
    return any(permission_matches(g, requested) for g in granted_permissions)
