"""Hosted deployment pieces: server config, publisher loop, signing keys, production tokens."""

from __future__ import annotations

import json
import stat
import threading
import uuid

import jwt
import pytest
from tasks_api.app import server_config
from tasks_api.auth import TokenVerifier, Unauthenticated
from tasks_api.publisher import PublishReport, run_loop

from empire_platform import automations
from empire_platform.devtoken import generate_keys, key_pair, mint

PEM = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"


def test_hosted_refuses_dev_issuer_and_missing_pem():
    with pytest.raises(SystemExit):
        server_config({"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM})
    with pytest.raises(SystemExit):
        server_config(
            {
                "EMPIRE_DB_MODE": "hosted",
                "TASKS_API_JWT_ISSUER": "empire-platform-dev",
                "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
            }
        )
    with pytest.raises(SystemExit):
        server_config(
            {"EMPIRE_DB_MODE": "hosted", "TASKS_API_JWT_ISSUER": "empire-prod"}
        )


def test_pem_env_wins_and_port_precedence(tmp_path):
    file = tmp_path / "public.pem"
    file.write_text("FILE")
    config = server_config(
        {
            "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
            "TASKS_API_JWT_PUBLIC_KEY": str(file),
            "TASKS_API_JWT_ISSUER": "empire-prod",
            "PORT": "8080",
            "TASKS_API_PORT": "9000",
            "TASKS_API_HOST": "0.0.0.0",
        }
    )
    assert (config.public_key_pem, config.issuer, config.port, config.host) == (
        PEM,
        "empire-prod",
        8080,
        "0.0.0.0",
    )
    dev = server_config(
        {"TASKS_API_JWT_PUBLIC_KEY": str(file), "TASKS_API_PORT": "9000"}
    )
    assert (dev.public_key_pem, dev.issuer, dev.port, dev.host) == (
        "FILE",
        "empire-platform-dev",
        9000,
        "127.0.0.1",
    )
    assert server_config({"TASKS_API_JWT_PUBLIC_KEY": str(file)}).port == 3601
    assert dev.max_lifetime == 3600
    hosted = server_config(
        {
            "EMPIRE_DB_MODE": "hosted",
            "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
            "TASKS_API_JWT_ISSUER": "empire-prod",
        }
    )
    assert hosted.max_lifetime == 30 * 86400


def test_loop_stops_on_iteration_limit_and_signal():
    lines: list[str] = []
    stop = threading.Event()
    assert (
        run_loop(
            lambda: PublishReport(delivered=2),
            0,
            stop,
            max_iterations=3,
            log=lines.append,
        )
        == 3
    )
    assert [json.loads(line) for line in lines] == [{"delivered": 2, "failed": 0}] * 3
    stop.set()
    assert run_loop(lambda: PublishReport(), 0, stop, log=lines.append) == 0


def test_loop_backs_off_on_errors_and_recovers():
    calls = {"n": 0}
    waits: list[float] = []
    lines: list[str] = []

    class FakeStop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    def step():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("secret-host.example")
        return PublishReport(delivered=1)

    run_loop(step, 5, FakeStop(), max_iterations=5, log=lines.append)
    assert waits[:3] == [10, 20, 40]
    assert waits[3:] == [5, 5]
    assert "secret-host" not in "".join(lines)
    assert json.loads(lines[0])["error"] == "ConnectionError"


def test_key_generation_permissions_and_no_overwrite(tmp_path):
    directory = tmp_path / "signing"
    public = generate_keys(directory)
    assert public == directory / "public.pem"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "private.pem").stat().st_mode) == 0o600
    before = (directory / "private.pem").read_bytes()
    with pytest.raises(FileExistsError):
        generate_keys(directory)
    assert (directory / "private.pem").read_bytes() == before
    assert automations.main(["keys", "--dir", str(directory)]) == 2


def test_cli_keys_prints_only_public_path(tmp_path, capsys):
    assert automations.main(["keys", "--dir", str(tmp_path / "k")]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "k" / "public.pem")
    assert "PRIVATE" not in out


def test_production_tokens(cluster, monkeypatch, tmp_path):
    from conftest import create_workspace

    from empire_platform.permissions import TASKS_OWNER

    with cluster.connect("platform_api") as conn:
        slug = f"prod-{uuid.uuid4().hex[:8]}"
        ws = create_workspace(conn, slug, list(TASKS_OWNER))
        conn.commit()
    monkeypatch.setattr(automations, "default_cluster", lambda: cluster)
    # Production order: a cell's owner permissions come first, then automations can be delegated them.
    from empire_platform import cells

    monkeypatch.setattr(cells, "default_cluster", lambda: cluster)
    assert (
        cells.main(["grant", "--workspace", slug, "--cell", "decisions", "--dry-run"])
        == 0
    )
    assert cells.main(["grant", "--workspace", slug, "--cell", "decisions"]) == 0
    assert cells.grant(cluster, ws.id, "decisions") == []  # idempotent
    assert cells.main(["grant", "--workspace", slug, "--cell", "habits"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "notifications"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "goals"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "projects"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "roles-skills"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "operating"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "intake"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "automation"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "content"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "learning"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "diary"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "opportunities"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "health"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "time"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "radar"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "finance-accounts"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "transactions"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "budgets"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "cash-flow"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "reconciliations"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "business-assets"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "investments"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "liabilities"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "subscriptions"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "tax"]) == 0
    assert cells.main(["grant", "--workspace", slug, "--cell", "accounts"]) == 0
    keys, tokens = tmp_path / "keys", tmp_path / "tokens"
    generate_keys(keys)
    assert (
        automations.main(
            [
                "provision",
                "--workspace",
                slug,
                "--tokens",
                str(tokens),
                "--key-dir",
                str(keys),
                "--issuer",
                "empire-prod",
                "--ttl-days",
                "30",
            ]
        )
        == 0
    )
    assert tokens.joinpath(
        "owner-cli.jwt"
    ).is_file()  # provisioned from the registry, with a delegation
    assert (
        automations.main(
            [
                "owner-token",
                "--workspace",
                slug,
                "--name",
                "owner-manual",
                "--tokens",
                str(tokens),
                "--key-dir",
                str(keys),
                "--issuer",
                "empire-prod",
                "--ttl-days",
                "30",
            ]
        )
        == 2
    )
    assert not tokens.joinpath("owner-manual.jwt").exists()
    assert (
        automations.main(
            [
                "owner-token",
                "--workspace",
                slug,
                "--name",
                "owner-manual",
                "--tokens",
                str(tokens),
                "--key-dir",
                str(keys),
                "--issuer",
                "empire-prod",
            ]
        )
        == 0
    )
    files = sorted(tokens.glob("*.jwt"))
    assert len(files) > 1
    assert tokens / "owner-manual.jwt" in files
    assert not [
        p for p in tokens.iterdir() if p.suffix != ".jwt"
    ]  # no leftover temp files
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        claims = jwt.decode(path.read_text(), options={"verify_signature": False})
        short = path.name == "owner-manual.jwt"
        lifetime = claims["exp"] - claims["iat"]
        assert claims["iss"] == "empire-prod"
        if short:
            assert lifetime == 3600
        else:  # bounded by the 30-day delegation it was minted under, never past it
            assert 30 * 86400 - 300 <= lifetime <= 30 * 86400
    owner = jwt.decode(
        (tokens / "owner-manual.jwt").read_text(), options={"verify_signature": False}
    )
    assert owner["sub"] == str(ws.owner)
    assert "del" not in owner
    bearer = "Bearer " + (tokens / "owner-manual.jwt").read_text()
    pub = (keys / "public.pem").read_text()
    prod = TokenVerifier(pub, "empire-prod", max_lifetime=30 * 86400)
    assert prod.verify(bearer, None).actor == ws.owner
    with pytest.raises(Unauthenticated):
        TokenVerifier(key_pair()[1], "empire-prod", max_lifetime=30 * 86400).verify(
            bearer, None
        )
    delegated = "Bearer " + (tokens / "owner-cli.jwt").read_text()
    assert prod.verify(delegated, None).delegation is not None
    with pytest.raises(
        Unauthenticated
    ):  # the default 1 hour cap still rejects a 30 day delegated token
        TokenVerifier(pub, "empire-prod").verify(delegated, None)


def _token(ttl, delegation=None):
    private, public = key_pair()
    obo = uuid.uuid4() if delegation else None
    token = mint(
        private,
        sub=uuid.uuid4(),
        ws=uuid.uuid4(),
        delegation=delegation,
        obo=obo,
        ttl=ttl,
        issuer="i",
    )
    return public, "Bearer " + token


def test_tokens_without_delegation_are_capped_at_one_hour_in_every_mode():
    public, long_owner = _token(3601)
    for cap in (3600, 30 * 86400):
        with pytest.raises(Unauthenticated, match="too long"):
            TokenVerifier(public, "i", max_lifetime=cap).verify(long_owner, None)
    public, ok = _token(3600)
    assert (
        TokenVerifier(public, "i", max_lifetime=30 * 86400).verify(ok, None).delegation
        is None
    )


def test_delegated_tokens_may_live_up_to_the_hosted_max():
    public, token = _token(30 * 86400, uuid.uuid4())
    assert (
        TokenVerifier(public, "i", max_lifetime=30 * 86400)
        .verify(token, None)
        .delegation
        is not None
    )
    with pytest.raises(Unauthenticated, match="too long"):
        TokenVerifier(public, "i", max_lifetime=7 * 86400).verify(token, None)
    public, over = _token(30 * 86400 + 1, uuid.uuid4())
    with pytest.raises(Unauthenticated, match="too long"):
        TokenVerifier(public, "i", max_lifetime=30 * 86400).verify(over, None)


def test_lifetime_has_a_hard_upper_cap():
    with pytest.raises(ValueError, match="max_lifetime must be between"):
        TokenVerifier(PEM, "i", max_lifetime=30 * 86400 + 1)
    with pytest.raises(SystemExit):
        server_config(
            {
                "EMPIRE_DB_MODE": "hosted",
                "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
                "TASKS_API_JWT_ISSUER": "empire-prod",
                "TASKS_API_JWT_MAX_LIFETIME_SECONDS": str(30 * 86400 + 1),
            }
        )
    ok = server_config(
        {
            "EMPIRE_DB_MODE": "hosted",
            "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
            "TASKS_API_JWT_ISSUER": "empire-prod",
            "TASKS_API_JWT_MAX_LIFETIME_SECONDS": str(30 * 86400),
        }
    )
    assert ok.max_lifetime == 30 * 86400


def test_hosted_refuses_the_dev_public_key():
    dev_pem = key_pair()[1]
    rewrapped = dev_pem.replace("\n", "\r\n")  # same key, different whitespace
    for pem in (dev_pem, rewrapped):
        with pytest.raises(SystemExit, match="dev public key"):
            server_config(
                {
                    "EMPIRE_DB_MODE": "hosted",
                    "TASKS_API_JWT_PUBLIC_KEY_PEM": pem,
                    "TASKS_API_JWT_ISSUER": "empire-prod",
                }
            )
    assert (
        server_config(
            {
                "EMPIRE_DB_MODE": "hosted",
                "TASKS_API_JWT_PUBLIC_KEY_PEM": PEM,
                "TASKS_API_JWT_ISSUER": "empire-prod",
            }
        ).public_key_pem
        == PEM
    )


def test_owner_cli_is_a_registered_automation():
    registry = automations.load_registry()
    owner = next(a for a in registry["automations"] if a["id"] == "owner-cli")
    assert set(owner["writers"]) == {
        "infra/cli.ts",
        "infra/scripts/cli/decision-cli.py",
        "infra/scripts/cli/habit-tracker.py",
        "infra/scripts/cli/routines-cli.py",
        "infra/scripts/cli/goals-cli.py",
        "infra/scripts/cli/projects-cli.py",
        "infra/scripts/cli/skills-cli.py",
        "infra/scripts/learning/learning_evidence.py",
        "infra/scripts/sync/sync-github.ts",
        "infra/scripts/utils/update-skills.ts",
        "infra/scripts/cli/data-intake-cli.py",
        "infra/scripts/cli/diary-intake-cli.py",
        "infra/scripts/cli/finance-intake-cli.py",
        "infra/scripts/cli/finance-review-cli.py",
        "infra/scripts/cli/finance-record-cli.py",
        "infra/scripts/workflows/finance_historical_recovery.py",
    }
    assert set(owner["scopes"]) <= {
        "tasks.task:read",
        "tasks.task:write",
        "tasks.status:write",
        "decisions.decision:read",
        "decisions.decision:write",
        "decisions.decision:approve",
        "decisions.decision:delete",
        "decisions.review:write",
        "habits.habit:read",
        "habits.habit:write",
        "habits.routine:read",
        "habits.routine:write",
        "notifications.rule:read",
        "notifications.rule:write",
        "notifications.notification:read",
        "notifications.notification:write",
        "goals.goal:read",
        "goals.goal:write",
        "goals.goal-task:read",
        "goals.goal-task:write",
        "projects.project:read",
        "projects.project:write",
        "projects.project-repo:read",
        "projects.project-repo:write",
        "roles-skills.progress-entry:read",
        "roles-skills.progress-entry:write",
        "roles-skills.skill:read",
        "roles-skills.skill:write",
        "roles-skills.skill-category:read",
        "intake.source:read",
        "intake.source:write",
        "intake.intake:read",
        "intake.intake:write",
        "intake.intake-event:write",
    }
    assert not any("cli" in item["writer"] for item in registry["not_automations"])


def test_default_ttl_is_one_hour(tmp_path):
    token = mint(key_pair()[0], sub=uuid.uuid4(), ws=uuid.uuid4(), ttl=3600)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 3600


def test_publisher_health_endpoint_follows_the_loop():
    import socket
    import threading
    import urllib.error
    import urllib.request

    from tasks_api.publisher import serve_health

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop = threading.Event()
    serve_health(port, stop)
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/health", timeout=5
    ) as response:
        assert response.status == 200
    stop.set()
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
    assert raised.value.code == 503


def _rsa_pair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

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


def test_rollover_key_verifies_next_to_the_current_key():
    old_private, old_public = _rsa_pair()
    new_private, new_public = _rsa_pair()
    _, stranger_public = _rsa_pair()
    verifier = TokenVerifier(
        old_public, "empire-prod", extra_public_key_pems=(new_public,)
    )
    for private in (old_private, new_private):
        token = mint(private, sub=uuid.uuid4(), ws=uuid.uuid4(), issuer="empire-prod")
        assert verifier.verify(f"Bearer {token}", None).channel == "api"
    stranger = TokenVerifier(stranger_public, "empire-prod")
    token = mint(new_private, sub=uuid.uuid4(), ws=uuid.uuid4(), issuer="empire-prod")
    with pytest.raises(Unauthenticated):
        stranger.verify(f"Bearer {token}", None)
    wrong_issuer = mint(
        new_private, sub=uuid.uuid4(), ws=uuid.uuid4(), issuer="someone-else"
    )
    with pytest.raises(Unauthenticated):
        verifier.verify(f"Bearer {wrong_issuer}", None)


def test_next_key_config_is_validated_and_published():
    current = _rsa_pair()[1].strip()
    nxt = _rsa_pair()[1].strip()
    base = {
        "EMPIRE_DB_MODE": "hosted",
        "TASKS_API_JWT_PUBLIC_KEY_PEM": current,
        "TASKS_API_JWT_ISSUER": "empire-prod",
    }
    assert server_config(base).main_public_key_pems == (current,)
    config = server_config({**base, "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM": nxt})
    assert config.main_public_key_pems == (current, nxt)
    with pytest.raises(SystemExit, match="differ from the current key"):
        server_config({**base, "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM": current})
    with pytest.raises(SystemExit, match="dev public key"):
        server_config({**base, "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM": key_pair()[1]})

    from empire_platform import jwks

    kids = {k["kid"] for k in jwks.jwk_set(*config.main_public_key_pems)["keys"]}
    assert kids == {jwks.rsa_jwk(current)["kid"], jwks.rsa_jwk(nxt)["kid"]}


def test_key_separation_compares_the_key_not_its_encoding():
    from cryptography.hazmat.primitives import serialization
    from tasks_api.app import _pem_digest

    private, spki = _rsa_pair()
    key = serialization.load_pem_private_key(private.encode(), None).public_key()
    pkcs1 = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
    ).decode()
    assert "RSA PUBLIC KEY" in pkcs1
    assert _pem_digest(pkcs1) == _pem_digest(spki)
    base = {
        "EMPIRE_DB_MODE": "hosted",
        "TASKS_API_JWT_PUBLIC_KEY_PEM": spki.strip(),
        "TASKS_API_JWT_ISSUER": "empire-prod",
    }
    with pytest.raises(SystemExit, match="differ"):
        server_config({**base, "TASKS_API_MCP_PUBLIC_KEY_PEM": pkcs1})
    with pytest.raises(SystemExit, match="differ from the current key"):
        server_config({**base, "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM": pkcs1})


def test_key_separation_also_covers_openssh_encoding():
    from cryptography.hazmat.primitives import serialization
    from tasks_api.app import _pem_digest

    private, spki = _rsa_pair()
    key = serialization.load_pem_private_key(private.encode(), None).public_key()
    ssh = key.public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()
    assert ssh.startswith("ssh-rsa ")
    assert _pem_digest(ssh) == _pem_digest(spki)
    base = {
        "EMPIRE_DB_MODE": "hosted",
        "TASKS_API_JWT_PUBLIC_KEY_PEM": spki.strip(),
        "TASKS_API_JWT_ISSUER": "empire-prod",
    }
    with pytest.raises(SystemExit, match="differ"):
        server_config({**base, "TASKS_API_MCP_PUBLIC_KEY_PEM": ssh})
    with pytest.raises(SystemExit, match="differ from the current key"):
        server_config({**base, "TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM": ssh})
