"""Mint short-lived platform tokens for local development only.

The key pair lives in ``.runtime/dev-keys`` (ignored by git) and is created on
first use. Hosted environments get tokens from the platform after the human
auth provider or the agent runtime has authenticated the caller.

    empire-dev-token --sub UUID --ws UUID [--del UUID --obo UUID] [--act UUID] [--ttl SECONDS]
"""

from __future__ import annotations

import argparse
import os
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from empire_platform.devdb import REPO_ROOT

KEY_DIR = REPO_ROOT / ".runtime" / "dev-keys"
ISSUER = "empire-platform-dev"
# Every token works for every cell API; each API still checks its own audience and asks the PDP.
DEFAULT_AUDIENCES = (
    "tasks-api",
    "graph-api",
    "platform-api",
    "decisions-api",
    "habits-api",
    "notifications-api",
    "goals-api",
    "projects-api",
    "roles-skills-api",
    "accounts-api",
    "operating-api",
    "intake-api",
    "automation-api",
    "content-api",
    "learning-api",
    "diary-api",
    "opportunities-api",
    "health-api",
    "time-api",
    "radar-api",
    "finance-accounts-api",
    "transactions-api",
    "budgets-api",
    "cash-flow-api",
    "reconciliations-api",
    "business-assets-api",
    "investments-api",
    "liabilities-api",
    "subscriptions-api",
    "tax-api",
)


def key_pair(directory: Path = KEY_DIR) -> tuple[str, str]:
    private_path, public_path = directory / "private.pem", directory / "public.pem"
    if not private_path.exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        private_path.chmod(0o600)
        public_path.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    return private_path.read_text(encoding="utf-8"), public_path.read_text(
        encoding="utf-8"
    )


def generate_keys(directory: Path) -> Path:
    """Create an RS256 pair: private key 0600 in a 0700 dir. Never overwrites; returns the public key path."""
    private_path, public_path = directory / "private.pem", directory / "public.pem"
    if private_path.exists() or public_path.exists():
        raise FileExistsError(
            f"keys already exist in {directory}; refusing to overwrite"
        )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return public_path


def mint(
    private_pem: str,
    *,
    sub: uuid.UUID,
    ws: uuid.UUID,
    delegation: uuid.UUID | None = None,
    obo: uuid.UUID | None = None,
    act: uuid.UUID | None = None,
    ttl: int = 600,
    issuer: str = ISSUER,
    audience: str | list[str] = DEFAULT_AUDIENCES,
) -> str:
    now = int(time.time())
    aud = audience if isinstance(audience, str) else list(audience)
    claims: dict = {
        "iss": issuer,
        "aud": aud,
        "sub": str(sub),
        "ws": str(ws),
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    if delegation:
        claims["del"] = str(delegation)
    if obo:
        claims["obo"] = str(obo)
    if act:
        claims["act"] = {"sub": str(act)}
    return jwt.encode(claims, private_pem, algorithm="RS256")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-dev-token", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--sub", type=uuid.UUID, required=True)
    parser.add_argument("--ws", type=uuid.UUID, required=True)
    parser.add_argument("--del", dest="delegation", type=uuid.UUID)
    parser.add_argument("--obo", type=uuid.UUID)
    parser.add_argument("--act", type=uuid.UUID)
    parser.add_argument("--ttl", type=int, default=600)
    args = parser.parse_args(argv)
    private_pem, _ = key_pair()
    print(
        mint(
            private_pem,
            sub=args.sub,
            ws=args.ws,
            delegation=args.delegation,
            obo=args.obo,
            act=args.act,
            ttl=args.ttl,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
