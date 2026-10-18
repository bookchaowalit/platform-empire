"""Hosted empire-mcp: MCP Streamable HTTP behind Clerk OAuth 2.1.

Claude (web, mobile, cloud agents) connects to ``<EMPIRE_MCP_PUBLIC_URL>/mcp``.
Clerk is the authorization server (Dynamic Client Registration with PKCE);
this process is only a resource server:

1. every request to ``/mcp`` needs a Clerk OAuth access token, verified with
   Clerk's Backend API verify endpoint (opaque tokens, instant revocation;
   positive results cached for at most 60 seconds, negative ones for 15, under
   a SHA-256 of the token). Malformed tokens never reach Clerk; failed
   attempts are rate limited per client IP (429). With
   ``EMPIRE_MCP_ALLOWED_CLIENTS`` set, only those OAuth client ids pass;
2. the Clerk user id must be in ``EMPIRE_MCP_SUBJECTS``. Anyone can sign up
   to Clerk, so this map is the gate: an unmapped subject gets 403 before
   any tool runs, and an empty or missing map allows nobody;
3. each tool call mints a fresh token with empire-mcp's own key (never the
   automation key) for the mapped platform identity and workspace, scoped to
   ``MCP_SCOPES``, at most 300 seconds, no delegation, and
   ``act.sub = "clerk:<user id>"``. The Tasks API trusts this issuer for
   nothing more.

Tokens are never logged; log lines carry status codes and reason codes only.

    empire-mcp-http                 # binds 0.0.0.0:$PORT; settings from the environment
    empire-mcp-http --check-clerk   # probe the Clerk verify path with a bogus token; prints no key
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from empire_mcp.server import StrictMCPServer, ToolFailed, build_server
from empire_platform.token_exchange import MCP_ISSUER, McpTokenMinter

CLERK_API_URL = "https://api.clerk.com/v1"  # Backend API base; CLERK_API_URL overrides
CLERK_TIMEOUT_SECONDS = 5.0
CACHE_MAX_SECONDS = 60
NEGATIVE_CACHE_SECONDS = 15
CACHE_MAX_ENTRIES = 10_000
MAX_TOKEN_LENGTH = 4096
FAILED_ATTEMPTS_PER_MINUTE = 30
MCP_PATH = "/mcp"
log = logging.getLogger("empire_mcp.hosted")


def _event(**fields: Any) -> None:
    """One structured line; codes and IDs only, never a token or a key."""
    log.info(json.dumps(fields, sort_keys=True, default=str))


@dataclass(frozen=True)
class Subject:
    identity: uuid.UUID
    workspace: uuid.UUID


def parse_subjects(raw: str | None) -> dict[str, Subject]:
    """EMPIRE_MCP_SUBJECTS: {"<clerk user id>": {"identity": uuid, "workspace": uuid}}. Empty allows nobody."""
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("EMPIRE_MCP_SUBJECTS is not valid JSON") from exc
    if not isinstance(data, dict):
        raise SystemExit("EMPIRE_MCP_SUBJECTS must be a JSON object")
    subjects: dict[str, Subject] = {}
    for user, entry in data.items():
        if (
            not user
            or not isinstance(entry, dict)
            or set(entry) != {"identity", "workspace"}
        ):
            raise SystemExit(
                "each EMPIRE_MCP_SUBJECTS entry needs exactly identity and workspace"
            )
        try:
            subjects[user] = Subject(
                uuid.UUID(str(entry["identity"])), uuid.UUID(str(entry["workspace"]))
            )
        except ValueError as exc:
            raise SystemExit(
                "EMPIRE_MCP_SUBJECTS identity and workspace must be UUIDs"
            ) from exc
    return subjects


def _epoch_seconds(value: Any) -> float | None:
    """Clerk timestamps may be seconds or milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / 1000 if value > 1e11 else float(value)


VERIFY_PATH = "/oauth_applications/access_tokens/verify"


def verify_urls(api_url: str) -> tuple[str, ...]:
    """The verify URL under the Backend API base, then (only after a 404) the same path without ``/v1``."""
    base = api_url.rstrip("/")
    urls = [base + VERIFY_PATH]
    if base.endswith("/v1"):
        urls.append(base[: -len("/v1")] + VERIFY_PATH)
    return tuple(urls)


def well_formed(token: str) -> bool:
    """Reject before calling Clerk: empty, longer than 4 KB, or any whitespace or control character."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return False
    return not any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in token)


class ClerkTokenVerifier:
    """mcp TokenVerifier backed by Clerk's OAuth access token verify endpoint."""

    def __init__(
        self,
        secret_key: str,
        *,
        issuer: str | None = None,
        api_url: str = CLERK_API_URL,
        client: httpx.AsyncClient | None = None,
        cache_seconds: float = CACHE_MAX_SECONDS,
        allowed_clients: frozenset[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret_key:
            raise ValueError("CLERK_SECRET_KEY is required")
        if not 0 <= cache_seconds <= CACHE_MAX_SECONDS:
            raise ValueError(f"cache_seconds must be between 0 and {CACHE_MAX_SECONDS}")
        self._secret_key = secret_key
        self._issuer = issuer
        self._urls = verify_urls(api_url)
        self._resolved: str | None = (
            None  # the verify URL that last verified a token (answered 200)
        )
        self._client = client or httpx.AsyncClient(timeout=CLERK_TIMEOUT_SECONDS)
        self._cache_seconds = cache_seconds
        self._allowed_clients = allowed_clients
        self._clock = clock
        self._cache: dict[str, tuple[float, AccessToken]] = {}
        self._negative: dict[str, float] = {}

    def __repr__(self) -> str:  # never show the secret key
        return f"ClerkTokenVerifier(urls={self._urls!r}, pinned_clients={self._allowed_clients is not None})"

    async def verify_token(self, token: str) -> AccessToken | None:
        if not well_formed(token):
            _event(event="mcp.auth.rejected", reason="malformed_token")
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None:
            if cached[0] > now:
                return cached[1]
            del self._cache[key]
        refused_until = self._negative.get(key)
        if refused_until is not None:
            if refused_until > now:
                return None
            del self._negative[key]
        accepted = await self._verify(token, now)
        if accepted is None:
            _remember(self._negative, key, now + NEGATIVE_CACHE_SECONDS, now)
            return None
        access, expires = accepted
        until = (
            now + self._cache_seconds
            if expires is None
            else min(now + self._cache_seconds, expires)
        )
        if until > now:
            _remember(self._cache, key, (until, access), now)
        return access

    async def _post(self, token: str) -> httpx.Response | None:
        """POST to the resolved verify URL; until one is known, fall back once past a 404."""
        urls = (self._resolved,) if self._resolved else self._urls
        response = None
        for url in urls:
            try:
                response = await self._client.post(
                    url,
                    json={"access_token": token},
                    timeout=CLERK_TIMEOUT_SECONDS,
                    headers={"Authorization": f"Bearer {self._secret_key}"},
                )
            except httpx.HTTPError as exc:
                _event(event="mcp.auth.verify_unavailable", error=type(exc).__name__)
                return None
            if response.status_code != 404:
                if response.status_code == 200:  # only a verified token proves the path
                    self._resolved = url
                return response
        return response

    async def _verify(
        self, token: str, now: float
    ) -> tuple[AccessToken, float | None] | None:
        response = await self._post(token)
        if response is None:
            return None
        if response.status_code != 200:  # 404 included: never success
            _event(
                event="mcp.auth.rejected",
                reason="verify_status",
                status=response.status_code,
            )
            return None
        try:
            body = response.json()
        except ValueError:
            body = None
        return self._accept(body, token, now)

    def _accept(
        self, body: Any, token: str, now: float
    ) -> tuple[AccessToken, float | None] | None:
        if not isinstance(body, dict):
            _event(event="mcp.auth.rejected", reason="malformed")
            return None
        if "object" in body and "oauth_access_token" not in str(body["object"]):
            _event(event="mcp.auth.rejected", reason="not_an_oauth_access_token")
            return None
        if body.get("revoked") or body.get("expired"):
            _event(
                event="mcp.auth.rejected",
                reason="revoked" if body.get("revoked") else "expired",
            )
            return None
        expires = _epoch_seconds(body.get("expiration", body.get("expires_at")))
        if expires is not None and expires <= now:
            _event(event="mcp.auth.rejected", reason="expired")
            return None
        client_id = body.get("client_id")
        client_id = client_id if isinstance(client_id, str) and client_id else None
        if self._allowed_clients is not None and client_id not in self._allowed_clients:
            _event(
                event="mcp.auth.rejected",
                reason="client_not_allowed",
                client_id=client_id,
            )
            return None
        subject = body.get("subject")
        if not isinstance(subject, str) or not subject:
            _event(event="mcp.auth.rejected", reason="no_subject")
            return None
        scopes = body.get("scopes")
        claims = {"iss": self._issuer} if self._issuer else None
        access = AccessToken(
            token=token,
            client_id=client_id or "unknown",
            subject=subject,
            scopes=[s for s in scopes if isinstance(s, str)]
            if isinstance(scopes, list)
            else [],
            expires_at=int(expires) if expires is not None else None,
            claims=claims,
        )
        return access, expires


def _remember(cache: dict[str, Any], key: str, value: Any, now: float) -> None:
    """Insert into a bounded cache whose values are an expiry or a tuple starting with one."""
    if len(cache) >= CACHE_MAX_ENTRIES:
        live = {
            k: v
            for k, v in cache.items()
            if (v[0] if isinstance(v, tuple) else v) > now
        }
        cache.clear()
        if len(live) < CACHE_MAX_ENTRIES:
            cache.update(live)
    cache[key] = value


def check_clerk(
    secret_key: str, api_url: str = CLERK_API_URL, client: httpx.Client | None = None
) -> tuple[int, list[str]]:
    """Probe each verify URL with a bogus token; report the path status only, never the key."""
    client = client or httpx.Client(timeout=CLERK_TIMEOUT_SECONDS)
    lines, found = [], False
    for url in verify_urls(api_url):
        try:
            response = client.post(
                url,
                json={"access_token": f"oat_check_{uuid.uuid4().hex}"},
                headers={"Authorization": f"Bearer {secret_key}"},
            )
        except httpx.HTTPError as exc:
            lines.append(f"{url}: unreachable ({type(exc).__name__})")
            continue
        if response.status_code == 404:
            lines.append(f"{url}: path not found (404{_error_codes(response)})")
        else:
            found = True
            lines.append(
                f"{url}: path ok ({response.status_code} for a bogus token{_error_codes(response)})"
            )
    return (0 if found else 1), lines


def _error_codes(response: httpx.Response) -> str:
    """Clerk error codes only (e.g. resource_not_found); never messages, which could echo input."""
    try:
        errors = response.json().get("errors") or []
        codes = sorted(
            {
                str(e.get("code"))
                for e in errors
                if isinstance(e, dict) and e.get("code")
            }
        )
    except (ValueError, AttributeError):
        return ""
    return f", codes: {', '.join(codes)}" if codes else ""


def _is_public(path: str) -> bool:
    return path == "/health" or path.startswith("/.well-known/")


class AllowlistMiddleware:
    """Runs after authentication: a valid Clerk token for an unmapped subject gets 403, before MCP runs."""

    def __init__(self, app: ASGIApp, subjects: Mapping[str, Subject]) -> None:
        self.app = app
        self.subjects = subjects

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _is_public(scope.get("path", "")):
            user = scope.get("user")
            if (
                isinstance(user, AuthenticatedUser)
                and user.access_token.subject not in self.subjects
            ):
                _event(event="mcp.denied", reason="subject_not_allowed")
                response = JSONResponse(
                    {
                        "error": "insufficient_access",
                        "error_description": "this account may not use this server",
                    },
                    status_code=403,
                    headers={
                        "WWW-Authenticate": 'Bearer error="insufficient_scope", '
                        'error_description="this account may not use this server"'
                    },
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def client_ip(scope: Scope, trust_forwarded: bool) -> str:
    """Left-most X-Forwarded-For entry when the platform proxy (Railway) sets it; else the socket peer."""
    if trust_forwarded:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                first = value.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
    client = scope.get("client")
    return client[0] if client else "unknown"


class FailedAttemptLimiter:
    """In-memory sliding window of failed (401/403) attempts per client IP; bounded in size."""

    def __init__(
        self,
        limit: int = FAILED_ATTEMPTS_PER_MINUTE,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = CACHE_MAX_ENTRIES,
    ) -> None:
        self.limit, self.window, self.clock, self.max_clients = (
            limit,
            window,
            clock,
            max_clients,
        )
        self._attempts: dict[str, deque[float]] = {}

    def _recent(self, ip: str, now: float) -> deque[float] | None:
        attempts = self._attempts.get(ip)
        if attempts is not None:
            while attempts and attempts[0] <= now - self.window:
                attempts.popleft()
            if not attempts:
                del self._attempts[ip]
                return None
        return attempts

    def blocked(self, ip: str) -> bool:
        attempts = self._recent(ip, self.clock())
        return attempts is not None and len(attempts) >= self.limit

    def record(self, ip: str) -> None:
        now = self.clock()
        attempts = self._recent(ip, now)
        if attempts is None:
            if len(self._attempts) >= self.max_clients:
                for other in list(self._attempts):
                    self._recent(other, now)
                if len(self._attempts) >= self.max_clients:
                    self._attempts.clear()
            attempts = self._attempts[ip] = deque()
        attempts.append(now)


class RateLimitMiddleware:
    """Outermost: over the failed-attempt limit an IP gets 429 before any token reaches Clerk."""

    def __init__(
        self, app: ASGIApp, limiter: FailedAttemptLimiter, trust_forwarded: bool
    ) -> None:
        self.app, self.limiter, self.trust_forwarded = app, limiter, trust_forwarded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_public(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        ip = client_ip(scope, self.trust_forwarded)
        if self.limiter.blocked(ip):
            _event(event="mcp.rate_limited")
            await JSONResponse(
                {"error": "too_many_requests"},
                status_code=429,
                headers={"Retry-After": str(int(self.limiter.window))},
            )(scope, receive, send)
            return

        async def observe(message: dict) -> None:
            if message["type"] == "http.response.start" and message["status"] in (
                401,
                403,
            ):
                self.limiter.record(ip)
            await send(message)

        await self.app(scope, receive, observe)


_caller: ContextVar[tuple[str, Subject] | None] = ContextVar(
    "empire_mcp_caller", default=None
)


def _access_token(context: Any) -> AccessToken | None:
    try:
        request = context.request_context.request if context is not None else None
    except (AttributeError, ValueError):
        request = None
    user = request.scope.get("user") if isinstance(request, Request) else None
    if isinstance(user, AuthenticatedUser):
        return user.access_token
    return get_access_token()


class HostedMCPServer(StrictMCPServer):
    """Refuses every tool call whose authenticated subject is not in the allowlist (defence in depth)."""

    def __init__(
        self, *args: Any, subjects: Mapping[str, Subject], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.subjects = dict(subjects)

    async def call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any], context: Any = None
    ):
        access = _access_token(context)
        subject = access.subject if access is not None else None
        mapped = self.subjects.get(subject) if subject else None
        if subject is None or mapped is None:
            _event(event="mcp.tool.denied", tool=name, reason="subject_not_allowed")
            raise ToolFailed("403: subject_not_allowed")
        reset = _caller.set((subject, mapped))
        try:
            return await super().call_tool(name, arguments, context)
        finally:
            _caller.reset(reset)


@dataclass(frozen=True)
class HostedConfig:
    public_url: str  # origin the clients use, without /mcp
    clerk_issuer_url: str
    clerk_secret_key: str
    signing_key_pem: str
    subjects: dict[str, Subject]
    tasks_api: str
    graph_api: str | None = None
    issuer: str = MCP_ISSUER
    port: int = 8000
    allowed_clients: frozenset[str] | None = (
        None  # pinned Clerk OAuth client ids; None = pinning off
    )
    trust_forwarded: bool = False  # True on Railway, whose proxy sets X-Forwarded-For
    clerk_api_url: str = CLERK_API_URL

    def __repr__(self) -> str:  # keep secrets out of tracebacks and logs
        return (
            f"HostedConfig(public_url={self.public_url!r}, clerk_issuer_url={self.clerk_issuer_url!r}, "
            f"subjects={len(self.subjects)}, tasks_api={self.tasks_api!r}, graph_api={self.graph_api!r})"
        )

    @property
    def resource_url(self) -> str:
        return self.public_url + MCP_PATH


def _https_url(name: str, value: str, *, allow_path: bool = False) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    local = parsed.hostname in ("localhost", "127.0.0.1")
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.netloc
        or (parsed.scheme == "http" and not local)
    ):
        raise SystemExit(f"{name} must be an https URL")
    if (parsed.path and not allow_path) or parsed.query or parsed.fragment:
        raise SystemExit(f"{name} must be an origin without a path")
    return value


def _cell_url(name: str, value: str) -> str:
    """https, or plain http only on Railway's private network (*.railway.internal) or loopback."""
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    host = parsed.hostname or ""
    private = host.endswith(".railway.internal") or host in (
        "localhost",
        "127.0.0.1",
        "::1",
    )
    if (
        not parsed.netloc
        or parsed.query
        or parsed.fragment
        or not (parsed.scheme == "https" or (parsed.scheme == "http" and private))
    ):
        raise SystemExit(
            f"{name} must be https unless the host is *.railway.internal or loopback"
        )
    return value


def parse_clients(raw: str | None) -> frozenset[str] | None:
    """EMPIRE_MCP_ALLOWED_CLIENTS: comma-separated Clerk OAuth client ids; empty turns pinning off."""
    clients = frozenset(part.strip() for part in (raw or "").split(",") if part.strip())
    return clients or None


def hosted_config(env: Mapping[str, str] | None = None) -> HostedConfig:
    env = os.environ if env is None else env
    missing = [
        name
        for name in (
            "EMPIRE_MCP_PUBLIC_URL",
            "CLERK_ISSUER_URL",
            "CLERK_SECRET_KEY",
            "EMPIRE_MCP_SIGNING_KEY_PEM",
            "EMPIRE_TASKS_API",
        )
        if not env.get(name, "").strip()
    ]
    if missing:
        raise SystemExit(f"hosted empire-mcp needs {', '.join(missing)}")
    subjects = parse_subjects(env.get("EMPIRE_MCP_SUBJECTS"))
    if not subjects:
        _event(
            event="mcp.config",
            warning="EMPIRE_MCP_SUBJECTS is empty: nobody may use tools",
        )
    clients = parse_clients(env.get("EMPIRE_MCP_ALLOWED_CLIENTS"))
    if clients is None:
        log.warning(
            json.dumps(
                {
                    "event": "mcp.config",
                    "warning": "client pinning is off: "
                    "EMPIRE_MCP_ALLOWED_CLIENTS is not set, any Clerk OAuth client is accepted",
                }
            )
        )
    else:
        _event(event="mcp.config", allowed_clients=sorted(clients))
    return HostedConfig(
        public_url=_https_url("EMPIRE_MCP_PUBLIC_URL", env["EMPIRE_MCP_PUBLIC_URL"]),
        clerk_issuer_url=_https_url("CLERK_ISSUER_URL", env["CLERK_ISSUER_URL"]),
        clerk_secret_key=env["CLERK_SECRET_KEY"].strip(),
        signing_key_pem=env["EMPIRE_MCP_SIGNING_KEY_PEM"].strip(),
        subjects=subjects,
        tasks_api=_cell_url("EMPIRE_TASKS_API", env["EMPIRE_TASKS_API"]),
        graph_api=_cell_url("EMPIRE_GRAPH_API", env["EMPIRE_GRAPH_API"])
        if env.get("EMPIRE_GRAPH_API", "").strip()
        else None,
        issuer=env.get("EMPIRE_MCP_ISSUER", "").strip() or MCP_ISSUER,
        port=int(env.get("PORT") or "8000"),
        allowed_clients=clients,
        trust_forwarded=bool(
            env.get("RAILWAY_ENVIRONMENT_ID") or env.get("RAILWAY_ENVIRONMENT")
        ),
        clerk_api_url=_https_url("CLERK_API_URL", env["CLERK_API_URL"], allow_path=True)
        if env.get("CLERK_API_URL", "").strip()
        else CLERK_API_URL,
    )


def build_app(
    config: HostedConfig,
    *,
    tasks: httpx.Client,
    graph: httpx.Client | None,
    verifier: Any,
    limiter: FailedAttemptLimiter | None = None,
) -> Starlette:
    """Tool handlers are synchronous; the SDK runs them in a worker thread, so cell calls never block the loop."""
    # The ASGI app: /mcp (rate limit + auth + allowlist + DNS-rebinding protection), /health, resource metadata.
    minter = McpTokenMinter(config.signing_key_pem, issuer=config.issuer)

    def mint(audience: str) -> str:
        caller = _caller.get()
        if (
            caller is None
        ):  # only reachable if a tool ran outside HostedMCPServer.call_tool
            raise ToolFailed("403: subject_not_allowed")
        subject, mapped = caller
        return minter.mint(
            audience, mapped.identity, mapped.workspace, f"clerk:{subject}"
        )

    auth = AuthSettings(
        issuer_url=config.clerk_issuer_url,
        resource_server_url=config.resource_url,
        validate_token_resource=False,
    )  # Clerk tokens are opaque; the allowlist is the gate
    server = HostedMCPServer(
        "empire",
        instructions="Solo Empire tasks. Acts as the authenticated caller only.",
        subjects=config.subjects,
        token_verifier=verifier,
        auth=auth,
    )
    build_server(tasks, graph, None, mint=mint, server=server)

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    public = urlparse(config.public_url)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc],
        allowed_origins=[f"{public.scheme}://{public.netloc}"],
    )
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host="0.0.0.0",
    )
    # Innermost user middleware: runs after the SDK's authentication sets scope["user"].
    app.user_middleware.append(
        Middleware(AllowlistMiddleware, subjects=config.subjects)
    )
    # Outermost: failed attempts are limited per client IP before a token is sent to Clerk.
    app.user_middleware.insert(
        0,
        Middleware(
            RateLimitMiddleware,
            limiter=limiter or FailedAttemptLimiter(),
            trust_forwarded=config.trust_forwarded,
        ),
    )
    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="empire-mcp-http", description="Hosted empire-mcp (Streamable HTTP)."
    )
    parser.add_argument(
        "--check-clerk",
        action="store_true",
        help="probe the Clerk verify path with a bogus token (needs CLERK_SECRET_KEY); prints no key",
    )
    args = parser.parse_args(argv)
    if args.check_clerk:
        secret = os.environ.get("CLERK_SECRET_KEY", "").strip()
        if not secret:
            print("CLERK_SECRET_KEY is not set", file=sys.stderr)
            return 2
        api_url = os.environ.get("CLERK_API_URL", "").strip() or CLERK_API_URL
        code, lines = check_clerk(secret, api_url)
        print("\n".join(lines))
        return code

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = hosted_config()
    tasks = httpx.Client(base_url=config.tasks_api, timeout=10.0)
    graph = (
        httpx.Client(base_url=config.graph_api, timeout=10.0)
        if config.graph_api
        else None
    )
    verifier = ClerkTokenVerifier(
        config.clerk_secret_key,
        issuer=config.clerk_issuer_url,
        api_url=config.clerk_api_url,
        allowed_clients=config.allowed_clients,
    )
    app = build_app(config, tasks=tasks, graph=graph, verifier=verifier)
    uvicorn.run(app, host="0.0.0.0", port=config.port)
    return 0
