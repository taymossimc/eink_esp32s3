from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .db import Database


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_enrollment_token() -> str:
    return secrets.token_urlsafe(32)


def parse_public_key(value: str) -> bytes:
    encoded = value.removeprefix("ed25519:")
    try:
        raw = b64url_decode(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid Ed25519 public key encoding") from exc
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    Ed25519PublicKey.from_public_bytes(raw)
    return raw


def key_id(raw_public_key: bytes) -> str:
    return f"ed25519:{hashlib.sha256(raw_public_key).hexdigest()[:24]}"


def load_or_create_signing_key(path: Path) -> tuple[Ed25519PrivateKey, str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise RuntimeError("edge signing key is not Ed25519")
        private = loaded
    else:
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise RuntimeError("edge signing key is not Ed25519") from None
            private = loaded
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(pem)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, key_id(public), f"ed25519:{b64url_encode(public)}"


def request_signing_bytes(
    timestamp: str, nonce: str, method: str, path: str, body: bytes
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\nsha256:{body_hash}".encode()


def verify_device_request(
    db: Database,
    *,
    device_id: str,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    method: str,
    path: str,
    body: bytes,
    window_seconds: int,
) -> None:
    if not timestamp or not nonce or not signature:
        raise PermissionError("missing device authentication headers")
    if len(nonce) < 16 or len(nonce) > 128:
        raise PermissionError("invalid nonce")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise PermissionError("invalid timestamp") from exc
    current = int(time.time())
    if abs(current - request_time) > window_seconds:
        raise PermissionError("request timestamp outside replay window")
    row = db.one(
        "SELECT public_key, status FROM devices WHERE device_id = ?",
        (device_id,),
    )
    if row is None or row["status"] != "active" or row["public_key"] is None:
        raise PermissionError("unknown or inactive device")
    encoded = signature.removeprefix("ed25519:")
    try:
        raw_signature = b64url_decode(encoded)
        Ed25519PublicKey.from_public_bytes(row["public_key"]).verify(
            raw_signature,
            request_signing_bytes(timestamp, nonce, method, path, body),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PermissionError("invalid device signature") from exc
    try:
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM nonces WHERE timestamp < ?", (current - window_seconds,)
            )
            connection.execute(
                "INSERT INTO nonces(device_id, nonce, timestamp) VALUES(?,?,?)",
                (device_id, nonce, request_time),
            )
    except sqlite3.IntegrityError as exc:
        raise PermissionError("replayed nonce") from exc


def verify_bearer(header: str | None, expected: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], expected)
