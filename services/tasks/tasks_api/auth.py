"""Verify platform-issued bearer tokens and turn them into a principal.

Tokens are short-lived RS256 JWTs issued by the platform after the human
auth provider (or the agent runtime) has authenticated the caller. Claims:

- ``sub``: identity performing the request
- ``ws``: workspace the request is bound to
- ``del`` / ``obo``: delegation and the identity it acts for (required together)
- ``act.sub``: initiating identity when it differs from ``sub`` (RFC 8693)

A second, narrowly trusted issuer may be configured for the hosted MCP server
(``empire-mcp``). Its tokens are signed with their own key and are accepted
only with a ``scope`` claim inside ``MCP_SCOPES``, without a delegation, and
for at most ``MCP_TOKEN_TTL_SECONDS``. Their ``act.sub`` names the external
OAuth subject (``clerk:<user id>``), not a platform identity, so the execution
is initiated by the mapped identity and the external subject travels as
``Principal.external_actor``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt

from empire_platform.token_exchange import MCP_SCOPES, MCP_TOKEN_TTL_SECONDS

AUDIENCE = "tasks-api"
MAX_LIFETIME_SECONDS = 3600  # tokens without a delegation, in every mode
HARD_MAX_LIFETIME_SECONDS = 30 * 86400  # no configuration may allow more than this
CHANNELS = {"api", "mcp", "workflow", "cli", "event"}
REQUIRED_CLAIMS = ["exp", "iat", "sub", "jti"]


class Unauthenticated(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    actor: uuid.UUID
    workspace: uuid.UUID
    initiated_by: uuid.UUID
    on_behalf_of: uuid.UUID
    delegation: uuid.UUID | None
    channel: str
    scopes: frozenset[str] | None = (
        None  # set only on exchanged tokens; narrows, never widens
    )
    external_actor: str | None = (
        None  # MCP-issued tokens only: the OAuth subject behind the call
    )


@dataclass(frozen=True)
class TokenVerifier:
    public_key_pem: str
    issuer: str
    audience: str = AUDIENCE
    max_lifetime: int = MAX_LIFETIME_SECONDS  # delegated tokens only (hosted: up to 30 days); every call re-checks the delegation
    mcp_issuer: str | None = (
        None  # second issuer, trusted only for MCP_SCOPES; off unless a key is given too
    )
    mcp_public_key_pem: str | None = None
    # Rollover keys for the main issuer: tokens signed by any of them verify too (key rotation).
    extra_public_key_pems: tuple[str, ...] = ()
    # Optional second gate: (identity, workspace) pairs MCP tokens may name; None = any mapped by empire-mcp.
    mcp_subjects: frozenset[tuple[uuid.UUID, uuid.UUID]] | None = None

    def __post_init__(self) -> None:
        if not 0 < self.max_lifetime <= HARD_MAX_LIFETIME_SECONDS:
            raise ValueError(
                f"max_lifetime must be between 1 and {HARD_MAX_LIFETIME_SECONDS} seconds"
            )
        if (self.mcp_issuer is None) != (self.mcp_public_key_pem is None):
            raise ValueError("mcp_issuer and mcp_public_key_pem must be given together")
        if self.mcp_issuer is not None and self.mcp_issuer == self.issuer:
            raise ValueError("the MCP issuer must differ from the main issuer")

    def verify(self, authorization: str | None, channel: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        token = authorization.removeprefix("Bearer ")
        if (
            self.mcp_issuer is not None
            and self._unverified_issuer(token) == self.mcp_issuer
        ):
            return self._verify_mcp(token, channel)
        try:
            claims = self._decode_main(token)
            lifetime = claims["exp"] - claims["iat"]
            # A token without a delegation (owner token) has no revocation record: always short-lived.
            limit = (
                self.max_lifetime
                if claims.get("del")
                else min(self.max_lifetime, MAX_LIFETIME_SECONDS)
            )
            if lifetime > limit:
                raise Unauthenticated("token lifetime too long")
            actor = uuid.UUID(claims["sub"])
            workspace = uuid.UUID(claims["ws"])
            delegation = uuid.UUID(claims["del"]) if claims.get("del") else None
            on_behalf_of = uuid.UUID(claims["obo"]) if claims.get("obo") else None
            initiated_by = (
                uuid.UUID(claims["act"]["sub"]) if claims.get("act") else actor
            )
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        if (delegation is None) != (on_behalf_of is None):
            raise Unauthenticated("delegation and obo must be given together")
        channel = channel or "api"
        if channel not in CHANNELS:
            raise Unauthenticated("unknown channel")
        scopes = (
            frozenset(str(claims["scope"]).split()) if claims.get("scope") else None
        )
        return Principal(
            scopes=scopes,
            actor=actor,
            workspace=workspace,
            initiated_by=initiated_by,
            on_behalf_of=on_behalf_of or actor,
            delegation=delegation,
            channel=channel,
        )

    def _decode_main(self, token: str) -> dict:
        """Verify with the main key, then each rollover key; only a bad signature moves on."""
        keys = (self.public_key_pem, *self.extra_public_key_pems)
        for key in keys[:-1]:
            try:
                return self._decode_with(token, key)
            except jwt.InvalidSignatureError:
                continue
        return self._decode_with(token, keys[-1])

    def _decode_with(self, token: str, key: str) -> dict:
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": REQUIRED_CLAIMS},
        )

    @staticmethod
    def _unverified_issuer(token: str) -> str | None:
        """Only selects which key to verify with; the chosen path verifies the issuer again."""
        try:
            issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError:
            return None
        return issuer if isinstance(issuer, str) else None

    def _verify_mcp(self, token: str, channel: str | None) -> Principal:
        # Only reached when mcp_issuer is set, and __post_init__ requires both or neither.
        assert self.mcp_public_key_pem is not None
        try:
            claims = jwt.decode(
                token,
                self.mcp_public_key_pem,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.mcp_issuer,
                options={"require": [*REQUIRED_CLAIMS, "scope"]},
            )
            if claims["exp"] - claims["iat"] > MCP_TOKEN_TTL_SECONDS:
                raise Unauthenticated("token lifetime too long")
            if "del" in claims or "obo" in claims:
                raise Unauthenticated("MCP tokens carry no delegation")
            scopes = frozenset(str(claims["scope"]).split())
            if not scopes or not scopes <= set(MCP_SCOPES):
                raise Unauthenticated("MCP token scope too wide")
            external = claims["act"]["sub"]
            if not isinstance(external, str) or not external:
                raise Unauthenticated("MCP token has no external actor")
            actor = uuid.UUID(claims["sub"])
            workspace = uuid.UUID(claims["ws"])
            if (
                self.mcp_subjects is not None
                and (actor, workspace) not in self.mcp_subjects
            ):
                raise Unauthenticated("MCP subject not allowed")
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        if (channel or "mcp") != "mcp":
            raise Unauthenticated("MCP tokens are used on the mcp channel only")
        return Principal(
            actor=actor,
            workspace=workspace,
            initiated_by=actor,
            on_behalf_of=actor,
            delegation=None,
            channel="mcp",
            scopes=scopes,
            external_actor=external,
        )
