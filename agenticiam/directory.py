"""The core directory: identities (users/agents/services), groups, roles,
role assignments, and API keys. This is the AD-equivalent "Users and
Computers" store, minus organizational units (a flat namespace plus groups
covers the same ground with far less operational overhead).
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import audit, crypto

VALID_KINDS = ("user", "agent", "service")


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class Directory:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------------------------------------------------------- identities
    def create_identity(
        self,
        kind: str,
        name: str,
        display_name: str = None,
        secret: str = None,
        metadata: dict = None,
        actor: dict = None,
    ) -> dict:
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind {kind!r}, must be one of {VALID_KINDS}")
        generated_secret = None
        if secret is None and kind in ("agent", "service"):
            generated_secret = crypto.generate_secret()
            secret = generated_secret
        secret_hash = crypto.hash_secret(secret) if secret else None

        identity_id = _new_id()
        try:
            self.conn.execute(
                "INSERT INTO identities (id, kind, name, display_name, secret_hash, "
                "enabled, created_at, metadata) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    identity_id,
                    kind,
                    name,
                    display_name or name,
                    secret_hash,
                    _now(),
                    json.dumps(metadata or {}),
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ConflictError(f"identity {name!r} already exists")

        audit.log(
            self.conn, "identity.create", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{name}", detail={"kind": kind},
        )
        result = self.get_identity(identity_id)
        if generated_secret:
            result["secret"] = generated_secret
        return result

    def _row_to_identity(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["metadata"] = json.loads(d.pop("metadata") or "{}")
        d.pop("secret_hash", None)
        return d

    def get_identity(self, name_or_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM identities WHERE id = ? OR name = ?", (name_or_id, name_or_id)
        ).fetchone()
        if not row:
            raise NotFoundError(f"identity {name_or_id!r} not found")
        return self._row_to_identity(row)

    def find_identity(self, name_or_id: str):
        try:
            return self.get_identity(name_or_id)
        except NotFoundError:
            return None

    def list_identities(self, kind: str = None) -> list:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM identities WHERE kind = ? ORDER BY name", (kind,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM identities ORDER BY name").fetchall()
        return [self._row_to_identity(r) for r in rows]

    def set_enabled(self, name_or_id: str, enabled: bool, actor: dict = None) -> dict:
        identity = self.get_identity(name_or_id)
        self.conn.execute(
            "UPDATE identities SET enabled = ? WHERE id = ?", (1 if enabled else 0, identity["id"])
        )
        self.conn.commit()
        audit.log(
            self.conn, "identity.set_enabled", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{identity['name']}", detail={"enabled": enabled},
        )
        return self.get_identity(identity["id"])

    def delete_identity(self, name_or_id: str, actor: dict = None) -> None:
        identity = self.get_identity(name_or_id)
        self.conn.execute("DELETE FROM identities WHERE id = ?", (identity["id"],))
        self.conn.commit()
        audit.log(
            self.conn, "identity.delete", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{identity['name']}",
        )

    def rotate_secret(self, name_or_id: str, actor: dict = None) -> str:
        identity = self.get_identity(name_or_id)
        new_secret = crypto.generate_secret()
        self.conn.execute(
            "UPDATE identities SET secret_hash = ? WHERE id = ?",
            (crypto.hash_secret(new_secret), identity["id"]),
        )
        self.conn.commit()
        audit.log(
            self.conn, "identity.rotate_secret", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{identity['name']}",
        )
        return new_secret

    def authenticate(self, name: str, secret: str) -> dict:
        row = self.conn.execute("SELECT * FROM identities WHERE name = ?", (name,)).fetchone()
        if not row or not row["enabled"] or not row["secret_hash"]:
            audit.log(self.conn, "identity.authenticate", "failure", actor_name=name)
            return None
        if not crypto.verify_secret(secret, row["secret_hash"]):
            audit.log(self.conn, "identity.authenticate", "failure", actor_name=name)
            return None
        audit.log(self.conn, "identity.authenticate", "success", actor_id=row["id"], actor_name=name)
        return self._row_to_identity(row)

    # ---------------------------------------------------------------- groups
    def create_group(self, name: str, description: str = None, actor: dict = None) -> dict:
        group_id = _new_id()
        try:
            self.conn.execute(
                "INSERT INTO groups_ (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                (group_id, name, description, _now()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ConflictError(f"group {name!r} already exists")
        audit.log(
            self.conn, "group.create", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"group:{name}",
        )
        return self.get_group(group_id)

    def get_group(self, name_or_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM groups_ WHERE id = ? OR name = ?", (name_or_id, name_or_id)
        ).fetchone()
        if not row:
            raise NotFoundError(f"group {name_or_id!r} not found")
        return dict(row)

    def list_groups(self) -> list:
        rows = self.conn.execute("SELECT * FROM groups_ ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def delete_group(self, name_or_id: str, actor: dict = None) -> None:
        group = self.get_group(name_or_id)
        self.conn.execute("DELETE FROM groups_ WHERE id = ?", (group["id"],))
        self.conn.commit()
        audit.log(
            self.conn, "group.delete", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"group:{group['name']}",
        )

    def add_member(self, group_name_or_id: str, identity_name_or_id: str, actor: dict = None) -> None:
        group = self.get_group(group_name_or_id)
        identity = self.get_identity(identity_name_or_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, identity_id) VALUES (?, ?)",
            (group["id"], identity["id"]),
        )
        self.conn.commit()
        audit.log(
            self.conn, "group.add_member", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"group:{group['name']}", detail={"member": identity["name"]},
        )

    def remove_member(self, group_name_or_id: str, identity_name_or_id: str, actor: dict = None) -> None:
        group = self.get_group(group_name_or_id)
        identity = self.get_identity(identity_name_or_id)
        self.conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND identity_id = ?",
            (group["id"], identity["id"]),
        )
        self.conn.commit()
        audit.log(
            self.conn, "group.remove_member", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"group:{group['name']}", detail={"member": identity["name"]},
        )

    def list_members(self, group_name_or_id: str) -> list:
        group = self.get_group(group_name_or_id)
        rows = self.conn.execute(
            "SELECT i.* FROM identities i "
            "JOIN group_members gm ON gm.identity_id = i.id "
            "WHERE gm.group_id = ? ORDER BY i.name",
            (group["id"],),
        ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def list_identity_groups(self, identity_name_or_id: str) -> list:
        identity = self.get_identity(identity_name_or_id)
        rows = self.conn.execute(
            "SELECT g.* FROM groups_ g "
            "JOIN group_members gm ON gm.group_id = g.id "
            "WHERE gm.identity_id = ? ORDER BY g.name",
            (identity["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- roles
    def create_role(self, name: str, description: str = None, actor: dict = None) -> dict:
        role_id = _new_id()
        try:
            self.conn.execute(
                "INSERT INTO roles (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                (role_id, name, description, _now()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ConflictError(f"role {name!r} already exists")
        audit.log(
            self.conn, "role.create", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"role:{name}",
        )
        return self.get_role(role_id)

    def get_role(self, name_or_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM roles WHERE id = ? OR name = ?", (name_or_id, name_or_id)
        ).fetchone()
        if not row:
            raise NotFoundError(f"role {name_or_id!r} not found")
        return dict(row)

    def list_roles(self) -> list:
        rows = self.conn.execute("SELECT * FROM roles ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def delete_role(self, name_or_id: str, actor: dict = None) -> None:
        role = self.get_role(name_or_id)
        self.conn.execute("DELETE FROM roles WHERE id = ?", (role["id"],))
        self.conn.commit()
        audit.log(
            self.conn, "role.delete", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"role:{role['name']}",
        )

    def grant_permission(self, role_name_or_id: str, permission: str, actor: dict = None) -> None:
        role = self.get_role(role_name_or_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO role_permissions (role_id, permission) VALUES (?, ?)",
            (role["id"], permission),
        )
        self.conn.commit()
        audit.log(
            self.conn, "role.grant_permission", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"role:{role['name']}", detail={"permission": permission},
        )

    def revoke_permission(self, role_name_or_id: str, permission: str, actor: dict = None) -> None:
        role = self.get_role(role_name_or_id)
        self.conn.execute(
            "DELETE FROM role_permissions WHERE role_id = ? AND permission = ?",
            (role["id"], permission),
        )
        self.conn.commit()
        audit.log(
            self.conn, "role.revoke_permission", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"role:{role['name']}", detail={"permission": permission},
        )

    def list_role_permissions(self, role_name_or_id: str) -> list:
        role = self.get_role(role_name_or_id)
        rows = self.conn.execute(
            "SELECT permission FROM role_permissions WHERE role_id = ? ORDER BY permission",
            (role["id"],),
        ).fetchall()
        return [r["permission"] for r in rows]

    def assign_role(
        self, role_name_or_id: str, principal_type: str, principal_name_or_id: str, actor: dict = None
    ) -> None:
        if principal_type not in ("identity", "group"):
            raise ValueError("principal_type must be 'identity' or 'group'")
        role = self.get_role(role_name_or_id)
        principal = (
            self.get_identity(principal_name_or_id)
            if principal_type == "identity"
            else self.get_group(principal_name_or_id)
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO role_assignments (role_id, principal_type, principal_id) "
            "VALUES (?, ?, ?)",
            (role["id"], principal_type, principal["id"]),
        )
        self.conn.commit()
        audit.log(
            self.conn, "role.assign", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"role:{role['name']}",
            detail={"principal_type": principal_type, "principal": principal["name"]},
        )

    def unassign_role(
        self, role_name_or_id: str, principal_type: str, principal_name_or_id: str, actor: dict = None
    ) -> None:
        role = self.get_role(role_name_or_id)
        principal = (
            self.get_identity(principal_name_or_id)
            if principal_type == "identity"
            else self.get_group(principal_name_or_id)
        )
        self.conn.execute(
            "DELETE FROM role_assignments WHERE role_id = ? AND principal_type = ? AND principal_id = ?",
            (role["id"], principal_type, principal["id"]),
        )
        self.conn.commit()
        audit.log(
            self.conn, "role.unassign", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"role:{role['name']}",
            detail={"principal_type": principal_type, "principal": principal["name"]},
        )

    def list_role_assignments(self, role_name_or_id: str) -> list:
        role = self.get_role(role_name_or_id)
        rows = self.conn.execute(
            "SELECT principal_type, principal_id FROM role_assignments WHERE role_id = ?",
            (role["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- effective permissions
    def effective_permissions(self, identity_name_or_id: str) -> set:
        identity = self.get_identity(identity_name_or_id)
        group_ids = [
            g["id"] for g in self.list_identity_groups(identity["id"])
        ]

        role_ids = set()
        rows = self.conn.execute(
            "SELECT role_id FROM role_assignments WHERE principal_type = 'identity' AND principal_id = ?",
            (identity["id"],),
        ).fetchall()
        role_ids.update(r["role_id"] for r in rows)

        if group_ids:
            placeholders = ",".join("?" * len(group_ids))
            rows = self.conn.execute(
                f"SELECT role_id FROM role_assignments WHERE principal_type = 'group' "
                f"AND principal_id IN ({placeholders})",
                group_ids,
            ).fetchall()
            role_ids.update(r["role_id"] for r in rows)

        if not role_ids:
            return set()

        placeholders = ",".join("?" * len(role_ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT permission FROM role_permissions WHERE role_id IN ({placeholders})",
            list(role_ids),
        ).fetchall()
        return {r["permission"] for r in rows}

    # ---------------------------------------------------------------- API keys
    def create_api_key(
        self, identity_name_or_id: str, scopes: list = None, ttl_seconds: int = None, actor: dict = None
    ) -> dict:
        identity = self.get_identity(identity_name_or_id)
        key_id = _new_id()
        plaintext = f"aiam_{key_id}_{crypto.generate_secret(24)}"
        expires_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
            if ttl_seconds
            else None
        )
        self.conn.execute(
            "INSERT INTO api_keys (id, identity_id, key_hash, scopes, expires_at, revoked, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                key_id,
                identity["id"],
                crypto.hash_secret(plaintext),
                json.dumps(scopes or []),
                expires_at,
                _now(),
            ),
        )
        self.conn.commit()
        audit.log(
            self.conn, "api_key.create", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"identity:{identity['name']}", detail={"key_id": key_id, "scopes": scopes},
        )
        return {"id": key_id, "key": plaintext, "identity": identity["name"], "scopes": scopes or [],
                "expires_at": expires_at}

    def verify_api_key(self, plaintext: str) -> dict:
        try:
            _, key_id, _rest = plaintext.split("_", 2)
        except ValueError:
            return None
        row = self.conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        if not row or row["revoked"]:
            return None
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        if not crypto.verify_secret(plaintext, row["key_hash"]):
            return None
        self.conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), key_id)
        )
        self.conn.commit()
        identity = self.get_identity(row["identity_id"])
        return {"identity": identity, "scopes": json.loads(row["scopes"]), "key_id": key_id}

    def revoke_api_key(self, key_id: str, actor: dict = None) -> None:
        self.conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
        self.conn.commit()
        audit.log(
            self.conn, "api_key.revoke", "success",
            actor_id=(actor or {}).get("id"), actor_name=(actor or {}).get("name"),
            resource=f"api_key:{key_id}",
        )

    def list_api_keys(self, identity_name_or_id: str = None) -> list:
        if identity_name_or_id:
            identity = self.get_identity(identity_name_or_id)
            rows = self.conn.execute(
                "SELECT id, identity_id, scopes, expires_at, revoked, created_at, last_used_at "
                "FROM api_keys WHERE identity_id = ? ORDER BY created_at DESC",
                (identity["id"],),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, identity_id, scopes, expires_at, revoked, created_at, last_used_at "
                "FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["scopes"] = json.loads(d["scopes"])
            d["revoked"] = bool(d["revoked"])
            result.append(d)
        return result
