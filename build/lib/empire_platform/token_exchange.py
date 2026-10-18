"""Token exchange prototype (RFC 8693 shape), local only.

A client such as the MCP adapter holds a subject token for audience
``empire-mcp``. Before calling a cell it exchanges that token for one bound
to exactly one audience, with a short lifetime and the narrowest scope set
the target accepts. The delegation, on-behalf-of, and actor chain are carried
over unchanged, so cells still authorize through the policy decision point;
the scope claim only narrows further.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import jwt

SUBJECT_AUDIENCE = "empire-mcp"
EXCHANGED_TTL_SECONDS = 300
# What each target accepts at most; a request narrower than this is honoured.
AUDIENCE_SCOPES = {
    "tasks-api": (
        "tasks.task:read",
        "tasks.task:write",
        "tasks.status:write",
        "tasks.calendar:write",
        "tasks.link:write",
        "tasks.archive:write",
        "tasks.reminder:write",
        "tasks.occurrence:write",
        "tasks.assignment:write",
        "tasks.suggestion:write",
    ),
    "graph-api": ("graph.query:read",),
}


class ExchangeRefused(Exception):
    pass


@dataclass(frozen=True)
class TokenExchanger:
    private_pem: str = field(repr=False)
    public_pem: str = field(repr=False)
    issuer: str

    def exchange(
        self, subject_token: str, audience: str, scopes: tuple[str, ...] | None = None
    ) -> str:
        if audience not in AUDIENCE_SCOPES:
            raise ExchangeRefused(f"audience {audience!r} is not exchangeable")
        try:
            claims = jwt.decode(
                subject_token,
                self.public_pem,
                algorithms=["RS256"],
                audience=SUBJECT_AUDIENCE,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise ExchangeRefused("invalid subject token") from exc
        allowed = AUDIENCE_SCOPES[audience]
        granted = tuple(s for s in (scopes or allowed) if s in allowed)
        if not granted:
            raise ExchangeRefused("no requested scope is available for this audience")
        now = int(time.time())
        exchanged = {
            "iss": self.issuer,
            "aud": audience,
            "sub": claims["sub"],
            "ws": claims["ws"],
            "iat": now,
            "exp": min(now + EXCHANGED_TTL_SECONDS, int(claims["exp"])),
            "jti": str(uuid.uuid4()),
            "scope": " ".join(granted),
            "act": claims.get("act") or {"sub": claims["sub"]},
        }
        for carried in ("del", "obo"):
            if claims.get(carried):
                exchanged[carried] = claims[carried]
        return jwt.encode(exchanged, self.private_pem, algorithm="RS256")


# Hosted MCP (empire-mcp) tokens. The MCP server authenticates the human with
# the OAuth provider, maps the subject to a platform identity through its
# allowlist, and mints one short token per tool call with its own key. Cells
# trust this issuer only for these scopes, without a delegation, for at most
# MCP_TOKEN_TTL_SECONDS: deleting and archiving are never reachable over MCP.
MCP_ISSUER = "empire-mcp"
MCP_TOKEN_TTL_SECONDS = 300
MCP_SCOPES = (
    "tasks.task:read",
    "tasks.task:write",
    "tasks.status:write",
    "tasks.link:write",
)
MCP_AUDIENCE_SCOPES = {"tasks-api": MCP_SCOPES, "graph-api": ("graph.query:read",)}


@dataclass(frozen=True)
class McpTokenMinter:
    """Mints the per-call token empire-mcp presents to a cell on the caller's behalf."""

    private_pem: str = field(repr=False)
    issuer: str = MCP_ISSUER
    ttl: int = MCP_TOKEN_TTL_SECONDS

    def __post_init__(self) -> None:
        if not 0 < self.ttl <= MCP_TOKEN_TTL_SECONDS:
            raise ValueError(
                f"MCP token ttl must be between 1 and {MCP_TOKEN_TTL_SECONDS} seconds"
            )

    def mint(
        self,
        audience: str,
        identity: uuid.UUID,
        workspace: uuid.UUID,
        external_subject: str,
    ) -> str:
        if audience not in MCP_AUDIENCE_SCOPES:
            raise ExchangeRefused(f"audience {audience!r} is not reachable over MCP")
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": audience,
            "sub": str(identity),
            "ws": str(workspace),
            "iat": now,
            "exp": now + self.ttl,
            "jti": str(uuid.uuid4()),
            "scope": " ".join(MCP_AUDIENCE_SCOPES[audience]),
            "act": {"sub": external_subject},
        }
        return jwt.encode(claims, self.private_pem, algorithm="RS256")
