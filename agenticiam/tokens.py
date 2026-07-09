"""Access token issuance and verification.

Tokens are HS256 JWTs signed with a per-installation symmetric key generated
on first run (see paths.signing_key_path). This is the same trust model as
Kerberos within an AD domain: the directory server is the sole party that
can mint or verify tokens, so there's no PKI to stand up for a single
self-hosted instance. Resource servers that aren't the directory itself
(e.g. an OpenClaw gateway) are expected to call POST /oauth/introspect
rather than verify JWTs locally.
"""

import base64
import hashlib
import hmac
import json
import time

from . import paths


class TokenError(Exception):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def load_signing_key() -> bytes:
    key_file = paths.signing_key_path()
    if key_file.exists():
        return bytes.fromhex(key_file.read_text().strip())
    import secrets

    key = secrets.token_bytes(32)
    key_file.write_text(key.hex())
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def encode(payload: dict, key: bytes = None) -> str:
    key = key or load_signing_key()
    header = {"alg": "HS256", "typ": "JWT"}
    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode()),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    segments.append(_b64url_encode(signature))
    return ".".join(segments)


def decode(token: str, key: bytes = None, verify_exp: bool = True) -> dict:
    key = key or load_signing_key()
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise TokenError("malformed token")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise TokenError("invalid signature")

    payload = json.loads(_b64url_decode(payload_b64))
    if verify_exp and "exp" in payload and payload["exp"] < time.time():
        raise TokenError("token expired")
    return payload


def issue_access_token(identity: dict, scopes, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": identity["id"],
        "name": identity["name"],
        "kind": identity["kind"],
        "scopes": sorted(scopes) if not isinstance(scopes, list) else scopes,
        "iat": now,
        "exp": now + ttl_seconds,
        "iss": "agenticiam",
    }
    return encode(payload)


def introspect(token: str, directory) -> dict:
    """Unified introspection: accepts either a JWT access token or an
    api_key-style opaque token, mirroring RFC 7662's token-agnostic shape."""
    if token.startswith("aiam_"):
        result = directory.verify_api_key(token)
        if not result:
            return {"active": False}
        return {
            "active": True,
            "sub": result["identity"]["id"],
            "name": result["identity"]["name"],
            "kind": result["identity"]["kind"],
            "scopes": result["scopes"] or sorted(directory.effective_permissions(result["identity"]["id"])),
        }

    try:
        payload = decode(token)
    except TokenError:
        return {"active": False}

    identity = directory.find_identity(payload["sub"])
    if not identity or not identity["enabled"]:
        return {"active": False}

    return {
        "active": True,
        "sub": payload["sub"],
        "name": payload["name"],
        "kind": payload["kind"],
        "scopes": payload.get("scopes", []),
        "exp": payload.get("exp"),
    }
