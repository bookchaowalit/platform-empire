"""tasks-api trusts platform-identity as a third issuer for the tasks and platform audiences.

Finance suite phase 0 (cellkit SPEC-004 counterpart): a cell forwards the caller's token to
``/v1/authz/decide`` and ``/v1/executions``, so the platform verifier must accept identity tokens too.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tasks_api.app import server_config
from tasks_api.auth import TokenVerifier, Unauthenticated

MAIN = "empire-platform"
IDENTITY = "https://identity.example"


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


MAIN_PRIVATE, MAIN_PUBLIC = _pair()
ID_PRIVATE, ID_PUBLIC = _pair()


def _token(
    private: str, iss: str, *, aud: Any = "platform-api", ttl: int = 300, **extra: Any
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": str(uuid.uuid4()),
        "ws": str(uuid.uuid4()),
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        **extra,
    }
    return "Bearer " + jwt.encode(claims, private, algorithm="RS256")


def _verifier(audience: str = "platform-api", identity: bool = True) -> TokenVerifier:
    return TokenVerifier(
        MAIN_PUBLIC,
        MAIN,
        audience=audience,
        identity_issuer=IDENTITY if identity else None,
        identity_public_key_pem=ID_PUBLIC if identity else None,
    )


def test_without_identity_trust_identity_tokens_are_refused():
    with pytest.raises(Unauthenticated):
        _verifier(identity=False).verify(_token(ID_PRIVATE, IDENTITY), None)


def test_an_identity_token_for_a_cell_and_the_platform_verifies_as_an_owner_style_principal():
    token = _token(ID_PRIVATE, IDENTITY, aud=["transactions-api", "platform-api"])
    claims = jwt.decode(
        token.removeprefix("Bearer "), options={"verify_signature": False}
    )
    principal = _verifier().verify(token, "api")
    assert (
        principal.actor
        == principal.initiated_by
        == principal.on_behalf_of
        == uuid.UUID(claims["sub"])
    )
    assert principal.workspace == uuid.UUID(claims["ws"])
    assert principal.delegation is None
    assert principal.scopes is None
    assert principal.external_actor is None
    # the main issuer is unaffected
    assert _verifier().verify(_token(MAIN_PRIVATE, MAIN), None)


def test_identity_scopes_narrow_like_exchanged_tokens():
    principal = _verifier("tasks-api").verify(
        _token(ID_PRIVATE, IDENTITY, aud="tasks-api", scope="tasks.task:read"), None
    )
    assert principal.scopes == frozenset({"tasks.task:read"})


def test_each_issuer_verifies_only_with_its_own_key():
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(MAIN_PRIVATE, IDENTITY), None)
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(ID_PRIVATE, MAIN), None)


@pytest.mark.parametrize(
    "extra",
    [
        {"del": str(uuid.uuid4()), "obo": str(uuid.uuid4())},
        {"act": {"sub": str(uuid.uuid4())}},
    ],
)
def test_identity_tokens_never_act_for_someone_else(extra):
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(ID_PRIVATE, IDENTITY, **extra), None)


def test_identity_tokens_are_short_lived_and_audience_bound():
    assert _verifier().verify(_token(ID_PRIVATE, IDENTITY, ttl=3600), None)
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(ID_PRIVATE, IDENTITY, ttl=3601), None)
    with pytest.raises(Unauthenticated):
        _verifier().verify(_token(ID_PRIVATE, IDENTITY, aud="transactions-api"), None)


def test_identity_trust_is_both_or_neither_and_distinct():
    with pytest.raises(ValueError, match="together"):
        TokenVerifier(MAIN_PUBLIC, MAIN, identity_issuer=IDENTITY)
    with pytest.raises(ValueError, match="differ"):
        TokenVerifier(
            MAIN_PUBLIC, MAIN, identity_issuer=MAIN, identity_public_key_pem=ID_PUBLIC
        )


BASE = {"TASKS_API_JWT_PUBLIC_KEY_PEM": MAIN_PUBLIC, "TASKS_API_JWT_ISSUER": MAIN}


def test_server_config_reads_identity_trust():
    assert server_config(BASE).identity_issuer is None
    config = server_config(
        {
            **BASE,
            "TASKS_API_IDENTITY_ISSUER": IDENTITY,
            "TASKS_API_IDENTITY_PUBLIC_KEY_PEM": ID_PUBLIC,
        }
    )
    assert (config.identity_issuer, config.identity_public_key_pem) == (
        IDENTITY,
        ID_PUBLIC.strip(),
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"TASKS_API_IDENTITY_ISSUER": IDENTITY}, "both"),
        ({"TASKS_API_IDENTITY_PUBLIC_KEY_PEM": ID_PUBLIC}, "both"),
        (
            {
                "TASKS_API_IDENTITY_ISSUER": MAIN,
                "TASKS_API_IDENTITY_PUBLIC_KEY_PEM": ID_PUBLIC,
            },
            "differ",
        ),
        (
            {
                "TASKS_API_IDENTITY_ISSUER": IDENTITY,
                "TASKS_API_IDENTITY_PUBLIC_KEY_PEM": MAIN_PUBLIC,
            },
            "key must differ",
        ),
    ],
)
def test_server_config_refuses_incomplete_or_shared_identity_trust(env, message):
    with pytest.raises(SystemExit, match=message):
        server_config({**BASE, **env})
