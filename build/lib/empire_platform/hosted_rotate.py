"""Hosted automation-token rotation: a Railway cron, not a laptop crontab.

Runs as ``EMPIRE_SERVICE=rotate`` in the platform-empire Railway project. It finds the
automations from ``deployments.json`` whose deployed token expires within
``rotate_within_days`` (or every one with ``EMPIRE_ROTATE_FORCE=1``), re-provisions
them with the signing key from the environment and pushes each fresh token over
the provider APIs:

* ``railway``    service variable (Railway GraphQL; the service redeploys)
* ``vercel``     production environment variable, then a redeploy of production
* ``cloudflare`` Worker secret (the remote MCP tool hub)
* ``infisical``  secret in a folder (owner CLIs and local MCP servers read it there)

No CLI and no checkout is needed. Token values travel only in request bodies and are
never printed; one JSON line per action goes to stdout. The run reports to the uptime
monitor's heartbeat (``token-rotate``) so a failed or missing run raises an alert.

Environment:
    EMPIRE_SIGNING_PRIVATE_KEY_PEM  issuer private key (Infisical /empire-platform)
    EMPIRE_TOKEN_ISSUER             default ``empire-platform``
    EMPIRE_ROTATE_WORKSPACE         default ``owner``
    EMPIRE_DB_MODE=hosted, EMPIRE_DB_URL_EMPIRE_MIGRATOR (workspace lookup), EMPIRE_DB_URL_PLATFORM_API
    RAILWAY_API_TOKEN, VERCEL_TOKEN (+ VERCEL_TEAM_ID), CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_ACCOUNT_ID, INFISICAL_CLIENT_ID + INFISICAL_CLIENT_SECRET (machine identity)
    or INFISICAL_TOKEN (an access token),
    INFISICAL_PROJECT_ID, INFISICAL_ENVIRONMENT (default dev)
    MONITOR_URL, HEARTBEAT_TOKEN    optional heartbeat
    EMPIRE_ROTATE_FORCE=1           rotate every target now
    EMPIRE_ROTATE_DRY_RUN=1         plan only
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from empire_platform.rotate import DAY, DEPLOYMENTS, emit, token_exp

RAILWAY_GRAPHQL = "https://backboard.railway.com/graphql/v2"
VERCEL_API = "https://api.vercel.com"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
INFISICAL_API = "https://app.infisical.com"
PROVIDERS = {"railway", "vercel", "cloudflare", "infisical"}
PREFLIGHT_PATH = "/empire-platform"
USER_AGENT = "empire-token-rotate/1 (+https://github.com/bookchaowalit/platform-empire)"


@dataclass(frozen=True)
class HostedTarget:
    automation: str
    provider: str
    variable: str
    project: str | None = None  # railway project name, vercel project name or id
    service: str | None = None  # railway service
    worker: str | None = None  # cloudflare worker script
    path: str | None = None  # infisical folder

    @property
    def label(self) -> str:
        where = self.service or self.worker or self.path or ""
        return f"{self.provider}:{self.project or ''}/{where}:{self.variable}"


def load_hosted_targets(path: Path = DEPLOYMENTS) -> tuple[list[HostedTarget], int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    targets: list[HostedTarget] = []
    for raw in data["targets"]:
        provider = raw["provider"]
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        target = HostedTarget(
            automation=raw["automation"],
            provider=provider,
            variable=raw["variable"],
            project=raw.get("project")
            or raw.get("vercel_project")
            or raw.get("checkout"),
            service=raw.get("service"),
            worker=raw.get("worker"),
            path=raw.get("path"),
        )
        missing = {
            "railway": target.service and target.project,
            "vercel": target.project,
            "cloudflare": target.worker,
            "infisical": target.path,
        }[provider]
        if not missing:
            raise ValueError(f"{provider} target for {target.automation} is incomplete")
        targets.append(target)
    return targets, int(data.get("rotate_within_days", 10))


class RailwayApi:
    """Project/service/environment lookup and variable read/upsert over GraphQL."""

    def __init__(self, client: httpx.Client, token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self._projects: dict[str, dict[str, Any]] | None = None

    def _query(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            RAILWAY_GRAPHQL,
            json={"query": query, "variables": dict(variables)},
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            # Provider messages can echo submitted values; keep only their error codes.
            codes = sorted(
                {
                    str((e.get("extensions") or {}).get("code") or "unknown")
                    for e in body["errors"]
                }
            )
            raise RuntimeError("railway graphql error: " + ",".join(codes))
        return body["data"]

    def _ids(self, project: str, service: str) -> tuple[str, str, str]:
        if self._projects is None:
            self._ids_cache_warm()
        assert self._projects is not None
        node = self._projects.get(project)
        if node is None:
            raise LookupError(f"railway project {project!r} not found")
        envs = {
            e["node"]["name"]: e["node"]["id"] for e in node["environments"]["edges"]
        }
        services = {
            s["node"]["name"]: s["node"]["id"] for s in node["services"]["edges"]
        }
        if "production" not in envs or service not in services:
            raise LookupError(f"railway service {project}/{service} not found")
        return node["id"], envs["production"], services[service]

    def verify(self) -> None:
        self._ids_cache_warm()

    def _ids_cache_warm(self) -> None:
        data = self._query(
            "query { projects { edges { node { id name"
            " environments { edges { node { id name } } }"
            " services { edges { node { id name } } } } } } }",
            {},
        )
        self._projects = {
            edge["node"]["name"]: edge["node"] for edge in data["projects"]["edges"]
        }

    def read(self, project: str, service: str, name: str) -> str | None:
        project_id, env_id, service_id = self._ids(project, service)
        data = self._query(
            "query($p: String!, $e: String!, $s: String) {"
            " variables(projectId: $p, environmentId: $e, serviceId: $s) }",
            {"p": project_id, "e": env_id, "s": service_id},
        )
        value = (data.get("variables") or {}).get(name)
        return value if isinstance(value, str) else None

    def upsert(self, project: str, service: str, name: str, value: str) -> None:
        project_id, env_id, service_id = self._ids(project, service)
        self._query(
            "mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }",
            {
                "input": {
                    "projectId": project_id,
                    "environmentId": env_id,
                    "serviceId": service_id,
                    "name": name,
                    "value": value,
                }
            },
        )


class VercelApi:
    def __init__(self, client: httpx.Client, token: str, team: str | None) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.params = {"teamId": team} if team else {}

    def verify(self, projects: tuple[str, ...] = ()) -> None:
        """Read each configured project: project-scoped tokens cannot read user resources."""
        for project in projects:
            self.client.get(
                f"{VERCEL_API}/v9/projects/{project}",
                params=self.params,
                headers=self.headers,
                timeout=30,
            ).raise_for_status()

    def upsert(self, project: str, name: str, value: str) -> None:
        response = self.client.post(
            f"{VERCEL_API}/v10/projects/{project}/env",
            params={**self.params, "upsert": "true"},
            json={
                "key": name,
                "value": value,
                "type": "encrypted",
                "target": ["production"],
            },
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        # A 2xx answer can still list per-variable failures.
        if (response.json() or {}).get("failed"):
            raise RuntimeError("vercel refused the environment variable")

    def redeploy(
        self, project: str, wait_seconds: float = 900, poll: float = 10
    ) -> None:
        listing = self.client.get(
            f"{VERCEL_API}/v6/deployments",
            params={
                **self.params,
                "projectId": project,
                "target": "production",
                "limit": 1,
                "state": "READY",
            },
            headers=self.headers,
            timeout=30,
        )
        listing.raise_for_status()
        deployments = listing.json().get("deployments") or []
        if not deployments:
            raise LookupError(
                f"vercel project {project!r} has no ready production deployment"
            )
        latest = deployments[0]
        response = self.client.post(
            f"{VERCEL_API}/v13/deployments",
            params=self.params,
            json={
                "name": latest["name"],
                "deploymentId": latest["uid"],
                "target": "production",
            },
            headers=self.headers,
            timeout=60,
        )
        response.raise_for_status()
        deployment = response.json().get("id")
        if not deployment:
            raise RuntimeError("vercel returned no deployment id")
        # The build runs asynchronously; production keeps the old token until it is READY.
        deadline = time.monotonic() + wait_seconds
        while True:
            status = self.client.get(
                f"{VERCEL_API}/v13/deployments/{deployment}",
                params=self.params,
                headers=self.headers,
                timeout=30,
            )
            status.raise_for_status()
            state = status.json().get("readyState")
            if state == "READY":
                return
            if state in {"ERROR", "CANCELED"}:
                raise RuntimeError(f"vercel deployment ended {state}")
            if time.monotonic() >= deadline:
                raise RuntimeError("vercel deployment did not become ready in time")
            time.sleep(poll)


def describe_error(exc: Exception) -> str:
    """Error text safe for logs: the type and HTTP status, never a provider message."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {str(exc)[:120]}"


class CloudflareApi:
    def __init__(self, client: httpx.Client, token: str, account: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.account = account

    def verify(self) -> None:
        """Account-owned and user-owned API tokens verify at different endpoints."""
        for url in (
            f"{CLOUDFLARE_API}/accounts/{self.account}/tokens/verify",
            f"{CLOUDFLARE_API}/user/tokens/verify",
        ):
            response = self.client.get(url, headers=self.headers, timeout=30)
            if (
                response.is_success
                and (response.json().get("result") or {}).get("status") == "active"
            ):
                return
        raise RuntimeError("cloudflare token is not active")

    def put_secret(self, worker: str, name: str, value: str) -> None:
        response = self.client.put(
            f"{CLOUDFLARE_API}/accounts/{self.account}/workers/scripts/{worker}/secrets",
            json={"name": name, "text": value, "type": "secret_text"},
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        if not response.json().get("success", False):
            raise RuntimeError(f"cloudflare refused the secret for {worker}")


class InfisicalApi:
    def __init__(
        self,
        client: httpx.Client,
        access_token: str,
        project: str,
        env: str,
    ) -> None:
        self.client = client
        self.project = project
        self.env = env
        self.headers = {"Authorization": f"Bearer {access_token}"}

    @classmethod
    def login(
        cls,
        client: httpx.Client,
        client_id: str,
        client_secret: str,
        project: str,
        env: str,
    ) -> InfisicalApi:
        """Universal Auth machine identity login."""
        response = client.post(
            f"{INFISICAL_API}/api/v1/auth/universal-auth/login",
            json={"clientId": client_id, "clientSecret": client_secret},
            timeout=30,
        )
        response.raise_for_status()
        return cls(client, response.json()["accessToken"], project, env)

    def verify(self) -> None:
        """Prove write access with a harmless canary secret: a read-only token must fail here,
        before any delegation is renewed."""
        self.upsert(
            PREFLIGHT_PATH,
            "ROTATE_PREFLIGHT_AT",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def upsert(self, path: str, name: str, value: str) -> None:
        body = {
            "workspaceId": self.project,
            "environment": self.env,
            "secretPath": path,
            "secretValue": value,
            "type": "shared",
        }
        url = f"{INFISICAL_API}/api/v3/secrets/raw/{name}"
        response = self.client.patch(url, json=body, headers=self.headers, timeout=30)
        if response.status_code == 404:
            response = self.client.post(
                url, json=body, headers=self.headers, timeout=30
            )
        response.raise_for_status()


@dataclass
class Apis:
    railway: RailwayApi | None = None
    vercel: VercelApi | None = None
    cloudflare: CloudflareApi | None = None
    infisical: InfisicalApi | None = None


def preflight(apis: Apis, targets: list[HostedTarget] | None = None) -> None:
    """Check every configured provider's credentials before anything is minted; a provider
    that fails is dropped, so the automations it serves are skipped, not revoked."""
    for provider in sorted(PROVIDERS):
        api = getattr(apis, provider)
        if api is None:
            continue
        try:
            if provider == "vercel":
                api.verify(
                    tuple(
                        sorted(
                            {
                                t.project
                                for t in targets or []
                                if t.provider == "vercel" and t.project
                            }
                        )
                    )
                )
            else:
                api.verify()
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
            setattr(apis, provider, None)
            emit(
                {
                    "action": "preflight",
                    "provider": provider,
                    "ok": False,
                    "error": describe_error(exc),
                }
            )


def due_hosted(
    targets: list[HostedTarget],
    apis: Apis,
    within_days: int,
    now: float,
    force: bool,
) -> dict[str, str]:
    """automation -> reason. Only Railway copies can be read back; the others rotate with them,
    or always when an automation has no Railway copy (its expiry is unknown)."""
    limit = now + within_days * DAY
    due: dict[str, str] = {}
    readable: set[str] = set()
    for target in targets:
        if force:
            due.setdefault(target.automation, "forced")
            continue
        if target.provider != "railway" or apis.railway is None:
            continue
        readable.add(target.automation)
        if target.automation in due:
            continue
        try:
            exp = token_exp(
                apis.railway.read(target.project, target.service, target.variable)
            )
        except (httpx.HTTPError, LookupError, RuntimeError) as exc:
            due[target.automation] = f"deployed token unreadable ({type(exc).__name__})"
            continue
        if exp is None or exp < limit:
            due[target.automation] = "deployed token missing or expiring"
    for target in targets:
        if target.automation not in readable and target.automation not in due:
            due[target.automation] = "expiry not readable; rotate every run"
    return due


def push(target: HostedTarget, token: str, apis: Apis) -> None:
    if target.provider == "railway":
        if apis.railway is None:
            raise RuntimeError("RAILWAY_API_TOKEN is not set")
        apis.railway.upsert(target.project, target.service, target.variable, token)
    elif target.provider == "vercel":
        if apis.vercel is None:
            raise RuntimeError("VERCEL_TOKEN is not set")
        apis.vercel.upsert(target.project, target.variable, token)
    elif target.provider == "cloudflare":
        if apis.cloudflare is None:
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID are not set"
            )
        apis.cloudflare.put_secret(target.worker, target.variable, token)
    else:
        if apis.infisical is None:
            raise RuntimeError("Infisical machine identity is not set")
        apis.infisical.upsert(target.path, target.variable, token)


def run(
    targets: list[HostedTarget],
    due: dict[str, str],
    mint: Callable[[list[str]], dict[str, str]],
    apis: Apis,
    dry_run: bool,
) -> int:
    """Mint the due automations and push them; returns the number of failures."""
    if not due:
        emit({"action": "none", "reason": "no token is due"})
        return 0
    for automation, reason in sorted(due.items()):
        emit(
            {
                "action": "plan" if dry_run else "mint",
                "automation": automation,
                "reason": reason,
            }
        )
    if dry_run:
        for target in targets:
            if target.automation in due:
                emit(
                    {
                        "action": "would-push",
                        "automation": target.automation,
                        "target": target.label,
                    }
                )
        return 0
    # Renewing a delegation revokes the deployed token, so an automation is minted only when
    # every copy can be delivered; otherwise the old token keeps working and the run fails.
    unreachable = {
        t.automation
        for t in targets
        if t.automation in due and getattr(apis, t.provider) is None
    }
    skipped = 0
    for target in targets:
        if target.automation in unreachable:
            skipped += 1
            emit(
                {
                    "action": "push",
                    "automation": target.automation,
                    "target": target.label,
                    "ok": False,
                    "error": "skipped: a destination of this automation is unavailable"
                    if getattr(apis, target.provider) is not None
                    else f"{target.provider} credentials are not available",
                }
            )
    mintable = sorted(set(due) - unreachable)
    tokens = mint(mintable) if mintable else {}
    failures = skipped
    vercel_projects: list[str] = []
    for target in targets:
        if target.automation not in due or target.automation in unreachable:
            continue
        token = tokens.get(target.automation)
        try:
            if not token:
                raise RuntimeError("no token minted")
            push(target, token, apis)
            ok, error = True, None
            if target.provider == "vercel" and target.project not in vercel_projects:
                vercel_projects.append(target.project)
        except (httpx.HTTPError, LookupError, RuntimeError) as exc:
            ok, error = False, describe_error(exc)
        failures += 0 if ok else 1
        event: dict[str, Any] = {
            "action": "push",
            "automation": target.automation,
            "target": target.label,
            "ok": ok,
        }
        if ok:
            event["expires"] = time.strftime(
                "%Y-%m-%d", time.gmtime(token_exp(token) or 0)
            )
        else:
            event["error"] = error
        emit(event)
    for project in vercel_projects:
        try:
            apis.vercel.redeploy(project)  # type: ignore[union-attr]
            ok, error = True, None
        except (httpx.HTTPError, LookupError, RuntimeError) as exc:
            ok, error = False, describe_error(exc)
        failures += 0 if ok else 1
        emit(
            {
                "action": "deploy",
                "provider": "vercel",
                "project": project,
                "ok": ok,
                **({"error": error} if error else {}),
            }
        )
    return failures


def build_apis(env: Mapping[str, str], client: httpx.Client) -> Apis:
    apis = Apis()
    if env.get("RAILWAY_API_TOKEN"):
        apis.railway = RailwayApi(client, env["RAILWAY_API_TOKEN"])
    if env.get("VERCEL_TOKEN"):
        apis.vercel = VercelApi(client, env["VERCEL_TOKEN"], env.get("VERCEL_TEAM_ID"))
    if env.get("CLOUDFLARE_API_TOKEN") and env.get("CLOUDFLARE_ACCOUNT_ID"):
        apis.cloudflare = CloudflareApi(
            client, env["CLOUDFLARE_API_TOKEN"], env["CLOUDFLARE_ACCOUNT_ID"]
        )
    project, environment = (
        env.get("INFISICAL_PROJECT_ID", ""),
        env.get("INFISICAL_ENVIRONMENT", "dev"),
    )
    if env.get("INFISICAL_TOKEN") and project:
        apis.infisical = InfisicalApi(
            client, env["INFISICAL_TOKEN"], project, environment
        )
    elif (
        env.get("INFISICAL_CLIENT_ID")
        and env.get("INFISICAL_CLIENT_SECRET")
        and project
    ):
        try:
            apis.infisical = InfisicalApi.login(
                client,
                env["INFISICAL_CLIENT_ID"],
                env["INFISICAL_CLIENT_SECRET"],
                project,
                environment,
            )
        except httpx.HTTPError as exc:
            # Only the Infisical copies fail (counted as push failures); the rest still rotate.
            emit(
                {
                    "action": "login",
                    "provider": "infisical",
                    "ok": False,
                    "error": describe_error(exc),
                }
            )
    return apis


def heartbeat(
    client: httpx.Client, env: Mapping[str, str], ok: bool, detail: str
) -> None:
    url, token = env.get("MONITOR_URL"), env.get("HEARTBEAT_TOKEN")
    if not url or not token:
        return
    try:
        client.post(
            f"{url.rstrip('/')}/heartbeat/token-rotate",
            json={"ok": ok, "detail": detail},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        emit({"action": "heartbeat", "ok": False})


def provision_mint(
    private_pem: str, issuer: str, workspace: str, ttl_days: float
) -> Callable[[list[str]], dict[str, str]]:
    """Re-provision through the existing CLI with a private, throw-away key and token dir."""

    def mint(automations: list[str]) -> dict[str, str]:
        from empire_platform import automations as provisioning

        with tempfile.TemporaryDirectory(prefix="empire-rotate-") as tmp:
            key_dir = Path(tmp) / "key"
            key_dir.mkdir(mode=0o700)
            key = key_dir / "private.pem"
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(private_pem)
            tokens_dir = Path(tmp) / "tokens"
            argv = [
                "provision",
                "--workspace",
                workspace,
                "--tokens",
                str(tokens_dir),
                "--ttl-days",
                str(ttl_days),
                "--key-dir",
                str(key_dir),
                "--issuer",
                issuer,
            ]
            for name in automations:
                argv += ["--only", name]
            if provisioning.main(argv) != 0:
                raise SystemExit("provisioning failed")
            return {
                name: (tokens_dir / f"{name}.jwt").read_text(encoding="utf-8").strip()
                for name in automations
                if (tokens_dir / f"{name}.jwt").is_file()
            }

    return mint


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    private_pem = env.get("EMPIRE_SIGNING_PRIVATE_KEY_PEM", "").strip()
    if not private_pem:
        raise SystemExit("EMPIRE_SIGNING_PRIVATE_KEY_PEM is not set")
    targets, within = load_hosted_targets()
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        try:
            apis = build_apis(env, client)
            dry_run = env.get("EMPIRE_ROTATE_DRY_RUN") == "1"
            if (
                not dry_run
            ):  # the Infisical check writes a canary; a dry run changes nothing
                preflight(apis, targets)
            due = due_hosted(
                targets,
                apis,
                within,
                time.time(),
                env.get("EMPIRE_ROTATE_FORCE") == "1",
            )
            failures = run(
                targets,
                due,
                provision_mint(
                    private_pem,
                    env.get("EMPIRE_TOKEN_ISSUER", "empire-platform"),
                    env.get("EMPIRE_ROTATE_WORKSPACE", "owner"),
                    float(env.get("EMPIRE_ROTATE_TTL_DAYS", "30")),
                ),
                apis,
                dry_run,
            )
        except (
            httpx.HTTPError,
            RuntimeError,
            LookupError,
            KeyError,
            SystemExit,
        ) as exc:
            heartbeat(client, env, False, f"{type(exc).__name__}")
            raise
        heartbeat(
            client,
            env,
            failures == 0,
            f"{len(due)} rotated, {failures} failure(s)" if due else "nothing due",
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
