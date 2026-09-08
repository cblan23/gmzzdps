#!/usr/bin/env python3
"""Provision the server-only Ed25519 key used to sign capture leases."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)


DEFAULT_KEY_PATH = Path("/etc/gmzz-dps-capability-signing-key.pem")


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    target = Path(path)
    if target.exists():
        key = serialization.load_pem_private_key(
            target.read_bytes(),
            password=None,
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("existing capability key is not Ed25519")
        return key

    target.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    payload = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return key


def public_configuration(key: Ed25519PrivateKey) -> dict[str, str]:
    raw_public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "capability_signing_key_id": (
            "lease-" + hashlib.sha256(raw_public_key).hexdigest()[:16]
        ),
        "capability_public_key": base64.b64encode(raw_public_key).decode("ascii"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_KEY_PATH)
    args = parser.parse_args()
    key = load_or_create_key(args.path)
    os.chmod(args.path, 0o600)
    print(json.dumps(public_configuration(key), separators=(",", ":")))


if __name__ == "__main__":
    main()
