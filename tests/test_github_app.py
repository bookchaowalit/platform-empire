"""GitHub App installation tokens (github_app) and their use by backlog-sync. Mocked HTTP only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from empire_platform import backlog_sync as sync
from empire_platform import github_app

NOW = 1_800_000_000.0
PERMISSIONS = {"issues": "read", "contents": "read", "metadata": "read"}


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: Any, fmt: serialization.PrivateFormat) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, fmt, serialization.NoEncryption()
    ).decode()


def pkcs1(key: rsa.RSAPrivateKey) -> str:
    return pem(key, serialization.PrivateFormat.TraditionalOpenSSL)


def pkcs8(key: Any) -> str:
    return pem(key, serialization.PrivateFormat.PKCS8)


def expiry(seconds: float) -> str:
    return datetime.fromtimestamp(NOW + seconds, UTC).isoformat().replace("+00:00", "Z")


class FakeGitHub:
    """Installations: ``orgs`` and ``users`` map an owner to an installation id."""

    def __init__(
        self,
        orgs: dict[str, int] | None = None,
        users: dict[str, int] | None = None,
        mint_status: int = 201,
        lifetime: float = 3600,
    ) -> None:
        self.orgs, self.users = orgs or {}, users or {}
        self.mint_status, self.lifetime = mint_status, lifetime
        self.mints: list[tuple[int, dict[str, Any]]] = []
        self.lookups: list[str] = []
        self.jwts: list[str] = []
        self.reads: list[tuple[str, str, str | None]] = []  # path, auth, q

    def __call__(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        parts = request.url.path.strip("/").split("/")
        if parts[-1] == "installation":
            self.jwts.append(auth.removeprefix("Bearer "))
            self.lookups.append(request.url.path)
            known = self.orgs if parts[0] == "orgs" else self.users
            if parts[1] in known:
                return httpx.Response(200, json={"id": known[parts[1]]})
            return httpx.Response(404, json={"message": "Not Found"})
        if parts[0] == "app":
            self.jwts.append(auth.removeprefix("Bearer "))
            installation = int(parts[2])
            self.mints.append((installation, json.loads(request.content)))
            if self.mint_status != 201:
                return httpx.Response(self.mint_status, json={"message": "nope"})
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_installation{installation}n{len(self.mints)}",
                    "expires_at": expiry(self.lifetime),
                },
            )
        self.reads.append((request.url.path, auth, request.url.params.get("q")))
        return httpx.Response(200, json={"items": []})


def make_app(handler: FakeGitHub, key: str, clock=lambda: NOW) -> github_app.GitHubApp:
    client = httpx.Client(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    )
    return github_app.GitHubApp("12345", key, PERMISSIONS, client, clock=clock)


# --- keys and the App JWT -----------------------------------------------------------


@pytest.mark.parametrize("encode", [pkcs1, pkcs8], ids=["pkcs1", "pkcs8"])
def test_jwt_is_rs256_with_backdated_iat_and_nine_minute_exp(rsa_key, encode):
    text = encode(rsa_key)
    assert ("BEGIN RSA PRIVATE KEY" in text) == (encode is pkcs1)
    key = github_app.load_private_key(text)
    token = github_app.app_jwt("12345", key, NOW)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token,
        rsa_key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iss": "12345", "iat": int(NOW) - 60, "exp": int(NOW) + 540}


def test_a_key_with_literal_backslash_n_sequences_loads(rsa_key):
    one_line = pkcs8(rsa_key).strip().replace("\n", "\\n")
    assert "\n" not in one_line
    assert github_app.load_private_key(one_line).key_size == 2048


def test_a_bad_or_non_rsa_key_is_refused_without_echoing_it():
    junk = "-----BEGIN PRIVATE KEY-----\nc2VrcmV0LWtleS1ieXRlcw==\n-----END PRIVATE KEY-----"
    with pytest.raises(ValueError, match="not an unencrypted PEM") as caught:
        github_app.load_private_key(junk)
    assert "c2VrcmV0" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    with pytest.raises(ValueError, match="not an RSA key"):
        github_app.load_private_key(pkcs8(ec.generate_private_key(ec.SECP256R1())))


# --- installation lookup and minting ------------------------------------------------


def test_installation_lookup_tries_the_org_then_the_user_and_caches(rsa_key):
    fake = FakeGitHub(orgs={"acme-org": 7}, users={"bookchaowalit": 3})
    app = make_app(fake, pkcs1(rsa_key))
    assert app.installation_id("acme-org") == 7
    assert app.installation_id("bookchaowalit") == 3
    assert app.installation_id("BookChaowalit") == 3  # owners ignore case; cached
    assert fake.lookups == [
        "/orgs/acme-org/installation",
        "/orgs/bookchaowalit/installation",
        "/users/bookchaowalit/installation",
    ]
    for token in fake.jwts:  # every App call is signed by the App
        claims = jwt.decode(
            token,
            rsa_key.public_key(),
            algorithms=["RS256"],
            options={"verify_iat": False},
        )
        assert claims["iss"] == "12345"


def test_an_owner_without_the_app_fails_with_the_owner_and_status_only(rsa_key):
    app = make_app(FakeGitHub(), pkcs1(rsa_key))
    with pytest.raises(github_app.GitHubAppError) as caught:
        app.token("nobody")
    assert str(caught.value) == (
        "GitHub App installation lookup for owner nobody failed: HTTP 404"
    )
    assert caught.value.status == 404


def test_the_token_is_down_scoped_and_reused_until_five_minutes_remain(rsa_key):
    now = [NOW]
    fake = FakeGitHub(orgs={"acme-org": 7})
    app = make_app(fake, pkcs8(rsa_key), clock=lambda: now[0])
    first = app.token("acme-org")
    assert fake.mints == [(7, {"permissions": PERMISSIONS})]
    now[0] = NOW + 3600 - 301  # 5 min 1 s left: still cached
    assert app.token("acme-org") == first
    now[0] = NOW + 3600 - 299  # under 5 min left: minted again
    second = app.token("acme-org")
    assert second != first
    assert len(fake.mints) == 2
    assert len(fake.lookups) == 1  # the installation id stays cached


def test_a_refused_mint_names_owner_and_status_but_no_secret(rsa_key):
    fake = FakeGitHub(orgs={"acme-org": 7}, mint_status=403)
    app = make_app(fake, pkcs1(rsa_key))
    with pytest.raises(github_app.GitHubAppError) as caught:
        app.token("acme-org")
    text = str(caught.value)
    assert text == "GitHub App token for owner acme-org failed: HTTP 403"
    assert fake.jwts
    assert all(t not in text for t in fake.jwts)


def test_app_from_env_needs_both_variables_or_none(rsa_key):
    client = httpx.Client()
    assert github_app.app_from_env({}, PERMISSIONS, client) is None
    with pytest.raises(ValueError, match=r"^GITHUB_APP_PRIVATE_KEY is unset"):
        github_app.app_from_env({"GITHUB_APP_ID": "1"}, PERMISSIONS, client)
    with pytest.raises(ValueError, match=r"^GITHUB_APP_ID is unset"):
        github_app.app_from_env(
            {"GITHUB_APP_PRIVATE_KEY": pkcs1(rsa_key)}, PERMISSIONS, client
        )
    with pytest.raises(ValueError, match=r"^GITHUB_APP_ID is not a numeric"):
        github_app.app_from_env(
            {"GITHUB_APP_ID": "abc", "GITHUB_APP_PRIVATE_KEY": pkcs1(rsa_key)},
            PERMISSIONS,
            client,
        )


# --- backlog-sync main() with the App -----------------------------------------------


@pytest.fixture
def routed(monkeypatch):
    """Every httpx.Client that main() builds talks to the returned FakeGitHub."""

    def install(fake: FakeGitHub) -> FakeGitHub:
        original = httpx.Client

        class Routed(original):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(fake)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", Routed)
        monkeypatch.setattr(sync, "SearchPace", lambda *a, **k: lambda: None)
        monkeypatch.setattr(github_app.time, "time", lambda: NOW)
        monkeypatch.setattr(sync, "capture", lambda exc: None)
        return fake

    return install


def run(env: dict[str, str], capsys) -> tuple[int, str]:
    code = sync.main(["--dry-run", "--sources", "github,files"], env=env)
    return code, capsys.readouterr().out


def test_main_uses_one_down_scoped_installation_token_per_owner(
    routed, rsa_key, capsys
):
    fake = routed(FakeGitHub(orgs={"acme-org": 7}, users={"bookchaowalit": 3}))
    env = {
        "GITHUB_APP_ID": "12345",
        "GITHUB_APP_PRIVATE_KEY": pkcs1(rsa_key).replace("\n", "\\n"),
        "BACKLOG_SYNC_GITHUB_OWNERS": "bookchaowalit,acme-org",
        "BACKLOG_SYNC_GITHUB_TOKEN": "pat-must-not-be-used",  # the App wins
    }
    code, out = run(env, capsys)
    assert code == 0, out
    assert sorted(fake.mints) == [
        (3, {"permissions": PERMISSIONS}),
        (7, {"permissions": PERMISSIONS}),
    ]  # one mint per owner, reused by every search of the run
    paths = {path for path, _, _ in fake.reads}
    assert paths == {"/search/issues", "/search/code"}
    for _, auth, query in fake.reads:
        assert "pat-must-not-be-used" not in auth
        owner = query.rsplit("user:", 1)[1]  # one owner per group: one installation
        installation = 3 if owner == "bookchaowalit" else 7
        assert auth.startswith(f"Bearer ghs_installation{installation}n")


def test_main_keeps_the_pat_when_the_app_is_not_configured(routed, capsys):
    fake = routed(FakeGitHub())
    env = {"BACKLOG_SYNC_GITHUB_TOKEN": "pat-value"}
    code, out = run(env, capsys)
    assert code == 0, out
    assert not fake.mints
    assert not fake.lookups
    assert fake.reads
    assert {auth for _, auth, _ in fake.reads} == {"Bearer pat-value"}


def test_main_reports_a_failed_mint_per_owner_without_secrets(routed, rsa_key, capsys):
    key = pkcs8(rsa_key)
    fake = routed(FakeGitHub(orgs={"acme-org": 7}, mint_status=403))
    env = {
        "GITHUB_APP_ID": "12345",
        "GITHUB_APP_PRIVATE_KEY": key,
        "BACKLOG_SYNC_GITHUB_OWNERS": "acme-org",
    }
    code, out = run(env, capsys)
    assert code == 1
    lines = [json.loads(line) for line in out.splitlines()]
    errors = [e for e in lines if e["event"] == "backlog.error"]
    assert {(e["source"], e["owners"], e["status"]) for e in errors} == {
        ("github", "acme-org", 403),
        ("files", "acme-org", 403),
    }
    assert not fake.reads  # nothing was read without a token
    body = key.splitlines()[1]
    for secret in (*fake.jwts, body, "PRIVATE KEY"):
        assert secret not in out


@pytest.mark.parametrize(
    ("extra", "detail"),
    [
        ({"GITHUB_APP_ID": "12345"}, "GITHUB_APP_PRIVATE_KEY is unset"),
        (
            {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY": "not-a-pem-sekrit"},
            "GITHUB_APP_PRIVATE_KEY is not an unencrypted PEM private key",
        ),
    ],
)
def test_main_refuses_a_half_or_broken_app_config_without_echo(
    routed, capsys, extra, detail
):
    routed(FakeGitHub())
    code, out = run({"BACKLOG_SYNC_GITHUB_TOKEN": "pat-value", **extra}, capsys)
    assert code == 2
    result = json.loads(out)
    assert result["error"] == "missing_config"
    assert result["detail"].startswith(detail)
    assert "sekrit" not in out
    assert "pat-value" not in out


def test_backlog_sync_requests_only_read_permissions():
    assert sync.GITHUB_APP_PERMISSIONS == {
        "issues": "read",
        "contents": "read",
        "metadata": "read",
    }
