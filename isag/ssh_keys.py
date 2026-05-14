"""Ed25519 keypair generation in OpenSSH format.

Produces files byte-identical to `ssh-keygen -t ed25519 -N "" -f <path>`,
without shelling out — keeps `isag run` portable to hosts that ship Docker
but not OpenSSH client tools.
"""
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def ensure_keypair(priv_path: Path) -> None:
    """Generate an ed25519 keypair at `priv_path` / `priv_path + '.pub'`.

    Idempotent: returns immediately if the private key already exists. This
    matters for both the user keypair (PyCharm pins it after first use) and
    the sshd host keypair (changing it after first connect triggers a host-
    key-mismatch error in every client).
    """
    pub_path = priv_path.with_name(priv_path.name + ".pub")
    if priv_path.exists():
        return

    priv_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()

    priv_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )

    priv_path.write_bytes(priv_bytes)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_bytes + b"\n")
    pub_path.chmod(0o644)
