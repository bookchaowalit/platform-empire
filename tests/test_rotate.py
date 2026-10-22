import base64
import json
import subprocess
from pathlib import Path

from empire_platform import rotate

NOW = 1_800_000_000.0


def jwt(exp: int) -> str:
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"iss": "empire-platform", "exp": exp}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.signature-not-checked"


class FakeRun:
    """Records CLI calls; answers `railway variables --json` from a dict of deployed values."""

    def __init__(
        self,
        deployed: dict[tuple[str, str], str] | None = None,
        fail: set[str] | None = None,
    ):
        self.deployed = deployed or {}
        self.fail = fail or set()
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def __call__(self, cmd, cwd=None, input=None, capture_output=False, text=False):
        self.calls.append((cmd, str(cwd) if cwd else None, input))
        if cmd[:2] == ["railway", "variables"] and "--json" in cmd:
            service = cmd[cmd.index("--service") + 1]
            values = {
                var: tok for (svc, var), tok in self.deployed.items() if svc == service
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(values), "")
        code = 1 if cmd[0] in self.fail else 0
        return subprocess.CompletedProcess(cmd, code, "", "")


def setup(tmp_path: Path, local_exp: int) -> tuple[list[rotate.Target], Path, Path]:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "goals-publisher.jwt").write_text(jwt(local_exp))
    (tokens / "solo-web.jwt").write_text(jwt(local_exp))
    checkouts = tmp_path / "checkouts"
    (checkouts / "goals").mkdir(parents=True)
    (checkouts / "owner-web").mkdir(parents=True)
    targets = [
        rotate.Target(
            "goals-publisher",
            "railway",
            "goals",
            "GOALS_PUBLISHER_TOKEN",
            "goals-publisher",
        ),
        rotate.Target(
            "goals-publisher", "railway", "goals", "GOALS_PUBLISHER_TOKEN", "goals-api"
        ),
        rotate.Target("solo-web", "vercel", "owner-web", "EMPIRE_CELL_TOKEN"),
    ]
    return targets, tokens, checkouts


def test_deployments_file_is_valid_and_covers_known_automations():
    targets, within = rotate.load_targets()
    assert within >= 7
    names = {t.automation for t in targets}
    assert {
        "goals-publisher",
        "reminder-worker",
        "recurring-gen",
        "solo-web",
        "accounts-web",
    } <= names
    registry = json.loads(
        (rotate.REPO_ROOT / "contracts/automations/v1/automations.json").read_text()
    )
    known = {a["id"] for a in registry["automations"]}
    assert names <= known


def test_nothing_due_when_local_and_deployed_tokens_are_fresh(tmp_path):
    fresh = int(NOW) + 20 * rotate.DAY
    targets, tokens, checkouts = setup(tmp_path, fresh)
    run = FakeRun(
        {
            ("goals-publisher", "GOALS_PUBLISHER_TOKEN"): jwt(fresh),
            ("goals-api", "GOALS_PUBLISHER_TOKEN"): jwt(fresh),
        }
    )
    assert rotate.due_automations(targets, tokens, checkouts, 10, run, NOW) == {}


def test_expiring_deployed_copy_makes_the_automation_due(tmp_path):
    fresh = int(NOW) + 20 * rotate.DAY
    targets, tokens, checkouts = setup(tmp_path, fresh)
    run = FakeRun(
        {
            ("goals-publisher", "GOALS_PUBLISHER_TOKEN"): jwt(
                int(NOW) + 3 * rotate.DAY
            ),
            ("goals-api", "GOALS_PUBLISHER_TOKEN"): jwt(fresh),
        }
    )
    due = rotate.due_automations(targets, tokens, checkouts, 10, run, NOW)
    assert due == {"goals-publisher": "deployed token missing or expiring"}


def test_rotation_mints_then_pushes_every_target_and_deploys_vercel_once(
    tmp_path, capsys
):
    targets, tokens, checkouts = setup(tmp_path, int(NOW) + 2 * rotate.DAY)
    new_exp = int(NOW) + 30 * rotate.DAY
    minted: list[list[str]] = []

    def mint(names):
        minted.append(names)
        for name in names:
            (tokens / f"{name}.jwt").write_text(jwt(new_exp))

    run = FakeRun()
    due = rotate.due_automations(targets, tokens, checkouts, 10, run, NOW)
    failures = rotate.rotate(targets, due, tokens, checkouts, mint, run, dry_run=False)
    assert failures == 0
    assert minted == [["goals-publisher", "solo-web"]]
    pushes = [
        c
        for c in run.calls
        if "--set-from-stdin" in c[0] or c[0][:3] == ["vercel", "env", "add"]
    ]
    assert len(pushes) == 3
    assert all(
        c[2] == jwt(new_exp) for c in pushes
    )  # the new token travels on stdin only
    assert [c[0] for c in run.calls if c[0][:2] == ["vercel", "deploy"]] == [
        ["vercel", "deploy", "--prod", "--yes"]
    ]
    out = capsys.readouterr().out
    assert jwt(new_exp) not in out
    assert "signature-not-checked" not in out


def test_dry_run_mints_and_pushes_nothing(tmp_path):
    targets, tokens, checkouts = setup(tmp_path, int(NOW) + 2 * rotate.DAY)
    run = FakeRun()
    due = rotate.due_automations(targets, tokens, checkouts, 10, run, NOW)

    def mint(_names):
        raise AssertionError("dry run must not mint")

    assert rotate.rotate(targets, due, tokens, checkouts, mint, run, dry_run=True) == 0
    assert not [
        c for c in run.calls if "--set-from-stdin" in c[0] or c[0][0] == "vercel"
    ]


def test_failed_push_is_counted(tmp_path):
    targets, tokens, checkouts = setup(tmp_path, int(NOW) + 2 * rotate.DAY)
    run = FakeRun(fail={"railway"})
    due = rotate.due_automations(targets, tokens, checkouts, 10, FakeRun(), NOW)
    failures = rotate.rotate(
        targets, due, tokens, checkouts, lambda names: None, run, dry_run=False
    )
    assert failures == 2  # both railway targets; vercel still pushed and deployed


def test_token_exp_ignores_garbage():
    assert rotate.token_exp(None) is None
    assert rotate.token_exp("not-a-jwt") is None
    assert rotate.token_exp("a.b.c") is None


def test_local_tokens_are_renewed_but_short_lived_ones_are_not(tmp_path):
    tokens = tmp_path / "tokens"
    tokens.mkdir()

    def write(name, iat, exp):
        payload = (
            base64.urlsafe_b64encode(json.dumps({"iat": iat, "exp": exp}).encode())
            .decode()
            .rstrip("=")
        )
        (tokens / f"{name}.jwt").write_text(f"h.{payload}.s")

    write("owner-cli", int(NOW) - 25 * rotate.DAY, int(NOW) + 5 * rotate.DAY)  # due
    write("governance-reader", int(NOW), int(NOW) + 30 * rotate.DAY)  # fresh
    write("import-owner", int(NOW), int(NOW) + 3600)  # short-lived: ignored
    write(
        "not-an-automation", int(NOW) - 25 * rotate.DAY, int(NOW) + rotate.DAY
    )  # unknown: ignored
    known = {"owner-cli", "governance-reader", "import-owner"}
    assert rotate.local_due(tokens, known, 10, NOW) == {
        "owner-cli": "local token expiring"
    }
    assert rotate.local_due(tokens, known, 10, NOW, skip={"owner-cli"}) == {}
