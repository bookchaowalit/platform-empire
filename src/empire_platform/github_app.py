"""GitHub App installation tokens, down-scoped per consumer (replaces fine-grained PATs).

Owner decision 2026-11-28: one GitHub App, installed on ``bookchaowalit`` and the
``bookchaowalit-*`` orgs, replaces the per-org PATs. A consumer that has
``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY`` mints, per owner, an installation token
limited to the permissions it asks for; without them it keeps its PAT.

1. App JWT: RS256, ``iat = now - 60``, ``exp = now + 540``, ``iss = GITHUB_APP_ID``.
2. Installation id: ``GET /orgs/{owner}/installation``, then ``/users/{owner}/installation``
   on 404. Cached per process.
3. ``POST /app/installations/{id}/access_tokens`` with ``{"permissions": ...}``.
4. Tokens last an hour; a cached token is re-minted when less than 5 minutes remain.

Nothing secret leaves this module: errors carry the owner and the HTTP status only, never
the key, the JWT or a token.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping
from datetime import datetime
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

API_VERSION = "2022-11-28"
USER_AGENT = "empire-platform-github-app"
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540
REMINT_MARGIN_SECONDS = 5 * 60
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": USER_AGENT,
}


class GitHubAppError(httpx.HTTPError):
    """Minting failed. The message names the owner and HTTP status, never a secret."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def load_private_key(pem: str) -> RSAPrivateKey:
    """The App key from PEM text: PKCS#1 or PKCS#8, literal ``\\n`` accepted as newlines.

    Raises ValueError with a fixed message: the key text never appears in it.
    """
    text = pem.strip().replace("\\n", "\n")
    try:
        key = load_pem_private_key(text.encode(), password=None)
    except (ValueError, TypeError):
        # from None: the original error may quote the input
        raise ValueError(
            "GITHUB_APP_PRIVATE_KEY is not an unencrypted PEM private key"
        ) from None
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("GITHUB_APP_PRIVATE_KEY is not an RSA key")
    return key


def app_jwt(app_id: str, key: RSAPrivateKey, now: float) -> str:
    issued = int(now)
    return jwt.encode(
        {
            "iat": issued - JWT_BACKDATE_SECONDS,
            "exp": issued + JWT_LIFETIME_SECONDS,
            "iss": app_id,
        },
        key,
        algorithm="RS256",
    )


class GitHubApp:
    """Mints and caches installation tokens for one App, down-scoped to ``permissions``."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        permissions: Mapping[str, str],
        client: httpx.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not app_id.strip().isdigit():
            raise ValueError("GITHUB_APP_ID is not a numeric App id")
        self.app_id = app_id.strip()
        self._key = load_private_key(private_key)
        self.permissions = dict(permissions)
        self._client = client  # base URL https://api.github.com, no default auth
        self._clock = clock
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}

    def _jwt_headers(self) -> dict[str, str]:
        return {
            **HEADERS,
            "Authorization": f"Bearer {app_jwt(self.app_id, self._key, self._clock())}",
        }

    def installation_id(self, owner: str) -> int:
        key = owner.lower()  # GitHub owners ignore case
        if key in self._installations:
            return self._installations[key]
        status = None
        for kind in ("orgs", "users"):
            response = self._client.get(
                f"/{kind}/{owner}/installation", headers=self._jwt_headers()
            )
            status = response.status_code
            if status == 200:
                self._installations[key] = int(response.json()["id"])
                return self._installations[key]
            if status != 404:
                break
        raise GitHubAppError(
            f"GitHub App installation lookup for owner {owner} failed: HTTP {status}",
            status,
        )

    def token(self, owner: str) -> str:
        installation = self.installation_id(owner)
        cached = self._tokens.get(installation)
        if cached and cached[1] - self._clock() > REMINT_MARGIN_SECONDS:
            return cached[0]
        response = self._client.post(
            f"/app/installations/{installation}/access_tokens",
            headers=self._jwt_headers(),
            json={"permissions": self.permissions},
        )
        if response.status_code != 201:
            raise GitHubAppError(
                f"GitHub App token for owner {owner} failed: "
                f"HTTP {response.status_code}",
                response.status_code,
            )
        body: dict[str, Any] = response.json()
        expires = datetime.fromisoformat(body["expires_at"]).timestamp()
        self._tokens[installation] = (body["token"], expires)
        return str(body["token"])


class InstallationAuth(httpx.Auth):
    """Sets the owner's installation token on every request, re-minted when it is due."""

    def __init__(self, app: GitHubApp, owner: str) -> None:
        self.app, self.owner = app, owner

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self.app.token(self.owner)}"
        yield request


def app_from_env(
    env: Mapping[str, str], permissions: Mapping[str, str], client: httpx.Client
) -> GitHubApp | None:
    """The App when both variables are set, None when neither is (keep the PAT).

    Raises ValueError (no secret in the message) when only one is set or a value is bad.
    """
    app_id = env.get("GITHUB_APP_ID", "").strip()
    key = env.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not app_id and not key:
        return None
    if not app_id or not key:
        missing = "GITHUB_APP_ID" if not app_id else "GITHUB_APP_PRIVATE_KEY"
        raise ValueError(f"{missing} is unset but the other GitHub App variable is set")
    return GitHubApp(app_id, key, permissions, client)
