"""Publish the platform's token verification key as a JWK Set (RFC 7517).

Cells can then fetch ``/.well-known/jwks.json`` instead of pinning the PEM in
their own environment, so the signing key can rotate without redeploying
every cell. Only public key material is ever serialized here.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key

JWKS_PATH = "/.well-known/jwks.json"


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def rsa_jwk(public_key_pem: str) -> dict[str, Any]:
    """RS256 verification JWK with its RFC 7638 thumbprint as ``kid``."""
    key = load_pem_public_key(public_key_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("the platform verification key must be an RSA public key")
    numbers = key.public_numbers()
    required = {"e": _b64(numbers.e), "kty": "RSA", "n": _b64(numbers.n)}
    thumbprint = hashlib.sha256(
        json.dumps(required, separators=(",", ":"), sort_keys=True).encode()
    ).digest()
    kid = base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode()
    return {**required, "alg": "RS256", "use": "sig", "kid": kid}


def jwk_set(*public_key_pems: str) -> dict[str, list[dict[str, Any]]]:
    keys: list[dict[str, Any]] = []
    for pem in public_key_pems:
        jwk = rsa_jwk(pem)
        if all(k["kid"] != jwk["kid"] for k in keys):
            keys.append(jwk)
    return {"keys": keys}


def register(app: Any, *public_key_pems: str) -> None:
    """Serve the JWK Set; public, cacheable for five minutes."""
    from fastapi.responses import JSONResponse

    body = jwk_set(*public_key_pems)

    @app.get(JWKS_PATH, include_in_schema=False)
    def jwks() -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})
