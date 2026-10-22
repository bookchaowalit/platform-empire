"""The published JWK Set verifies exactly the tokens the platform key signs."""

from __future__ import annotations

import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from empire_platform import jwks
from empire_platform.devtoken import mint


def _pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


def test_jwk_verifies_a_platform_token_and_holds_no_private_material():
    private, public = _pair()
    jwk = jwks.rsa_jwk(public)
    assert set(jwk) == {"kty", "n", "e", "alg", "use", "kid"}
    token = mint(
        private,
        sub=uuid.uuid4(),
        ws=uuid.uuid4(),
        issuer="empire-platform",
        audience="goals-api",
    )
    key = jwt.PyJWK(jwk).key
    claims = jwt.decode(
        token, key, algorithms=["RS256"], audience="goals-api", issuer="empire-platform"
    )
    assert claims["iss"] == "empire-platform"


def test_kid_is_the_stable_rfc7638_thumbprint():
    _, public = _pair()
    assert jwks.rsa_jwk(public)["kid"] == jwks.rsa_jwk(public)["kid"]
    _, other = _pair()
    assert jwks.rsa_jwk(public)["kid"] != jwks.rsa_jwk(other)["kid"]


def test_rfc7638_example_thumbprint():
    # RFC 7638 section 3.1 example key and its expected thumbprint.
    n = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMs"
        "tn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5h"
        "ajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    )
    e = 65537
    pem = (
        rsa.RSAPublicNumbers(e, int.from_bytes(jwt.utils.base64url_decode(n), "big"))
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    assert jwks.rsa_jwk(pem)["kid"] == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_non_rsa_key_is_refused():
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    with pytest.raises(ValueError, match="must be an RSA public key"):
        jwks.rsa_jwk(pem)


def test_route_serves_the_set_with_cache_headers():
    _, public = _pair()
    app = FastAPI()
    jwks.register(app, public, public)
    response = TestClient(app).get(jwks.JWKS_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.json() == {"keys": [jwks.rsa_jwk(public)]}
