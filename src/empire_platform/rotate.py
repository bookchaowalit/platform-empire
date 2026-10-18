"""Rotate automation tokens and push them to where they are deployed.

Automation tokens live 30 days. This command finds the automations whose token is
due (the local token file, or a deployed Railway copy, expires within
``rotate_within_days``), re-provisions only those automations (renewing their
delegation and minting fresh tokens into the token directory) and pushes each new
token to every target listed in ``contracts/automations/v1/deployments.json``:
Railway service variables (which redeploys the service) and Vercel production
environment variables (followed by one production deploy per project).

Token values are read and written through stdin and memory only; nothing secret is
printed. One JSON line per action goes to stdout.

    empire-token-rotate --workspace owner --tokens ~/.config/empire-platform/tokens \\
        --key-dir ~/.config/empire-platform/signing --issuer empire-platform [--dry-run] [--force]

Needs EMPIRE_DB_MODE=hosted (provisioning writes delegations), the Railway and
Vercel CLIs logged in, and the Book Dev checkouts under EMPIRE_CHECKOUTS_ROOT
(default: the folder that holds this repository).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENTS = REPO_ROOT / "contracts" / "automations" / "v1" / "deployments.json"
DAY = 86400
HOSTED_ONLY = frozenset({"cloudflare", "infisical"})

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Target:
    automation: str
    provider: str
    checkout: str
    variable: str
    service: str | None = None


def load_targets(path: Path = DEPLOYMENTS) -> tuple[list[Target], int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for t in data["targets"]:
        if t["provider"] not in {"railway", "vercel", *HOSTED_ONLY}:
            raise ValueError(f"unknown provider {t['provider']!r}")
    # Cloudflare and Infisical copies are pushed only by the hosted rotation (hosted_rotate).
    targets = [
        Target(
            automation=t["automation"],
            provider=t["provider"],
            checkout=t["checkout"],
            variable=t["variable"],
            service=t.get("service"),
        )
        for t in data["targets"]
        if t["provider"] not in HOSTED_ONLY
    ]
    for target in targets:
        if target.provider == "railway" and not target.service:
            raise ValueError(f"railway target for {target.automation} needs a service")
    return targets, int(data.get("rotate_within_days", 10))


def token_exp(token: str | None) -> int | None:
    """The exp claim of a JWT (no verification; only the expiry is used)."""
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, TypeError):
        return None


def read_token_file(tokens: Path, automation: str) -> str | None:
    path = tokens / f"{automation}.jwt"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def railway_value(run: Runner, cwd: Path, service: str, variable: str) -> str | None:
    completed = run(
        ["railway", "variables", "--service", service, "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout).get(variable)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def due_automations(
    targets: list[Target],
    tokens: Path,
    checkouts: Path,
    within_days: int,
    run: Runner,
    now: float,
    force: bool = False,
) -> dict[str, str]:
    """automation -> reason, for every automation whose token must be rotated."""
    limit = now + within_days * DAY
    due: dict[str, str] = {}
    for target in targets:
        if target.automation in due:
            continue
        if force:
            due[target.automation] = "forced"
            continue
        local_exp = token_exp(read_token_file(tokens, target.automation))
        if local_exp is None or local_exp < limit:
            due[target.automation] = "local token missing or expiring"
            continue
        if target.provider == "railway":
            deployed_exp = token_exp(
                railway_value(
                    run, checkouts / target.checkout, target.service, target.variable
                )
            )
            if deployed_exp is None or deployed_exp < limit:
                due[target.automation] = "deployed token missing or expiring"
    return due


def local_due(
    tokens: Path,
    known: set[str],
    within_days: int,
    now: float,
    skip: set[str] = frozenset(),
) -> dict[str, str]:
    """Token files used only on this machine (CLIs, MCP, monitors) that expire soon.

    Only automations from the registry count; short-lived tokens (a lifetime of a day or
    less, e.g. owner tokens) are left alone.
    """
    limit = now + within_days * DAY
    due: dict[str, str] = {}
    for path in sorted(tokens.glob("*.jwt")):
        name = path.stem
        if name in skip or name not in known:
            continue
        token = path.read_text(encoding="utf-8").strip()
        exp = token_exp(token)
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            iat = int(json.loads(base64.urlsafe_b64decode(payload)).get("iat") or 0)
        except (IndexError, ValueError, TypeError):
            iat = 0
        if exp is None or (iat and exp - iat <= DAY):
            continue
        if exp < limit:
            due[name] = "local token expiring"
    return due


def push_railway(
    run: Runner, cwd: Path, service: str, variable: str, token: str
) -> bool:
    completed = run(
        ["railway", "variables", "--service", service, "--set-from-stdin", variable],
        cwd=cwd,
        input=token,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def push_vercel(run: Runner, cwd: Path, variable: str, token: str) -> bool:
    completed = run(
        ["vercel", "env", "add", variable, "production", "--force"],
        cwd=cwd,
        input=token,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def deploy_vercel(run: Runner, cwd: Path) -> bool:
    completed = run(
        ["vercel", "deploy", "--prod", "--yes"], cwd=cwd, capture_output=True, text=True
    )
    return completed.returncode == 0


def emit(event: dict[str, Any]) -> None:
    print(json.dumps({"event": "token.rotate", **event}, sort_keys=True), flush=True)


def rotate(
    targets: list[Target],
    due: dict[str, str],
    tokens: Path,
    checkouts: Path,
    mint: Callable[[list[str]], None],
    run: Runner,
    dry_run: bool,
) -> int:
    """Mint the due automations, push their tokens; returns the number of failures."""
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
                        "provider": target.provider,
                        "checkout": target.checkout,
                        "service": target.service,
                        "variable": target.variable,
                    }
                )
        return 0
    mint(sorted(due))
    failures = 0
    vercel_checkouts: list[str] = []
    for target in targets:
        if target.automation not in due:
            continue
        token = read_token_file(tokens, target.automation)
        cwd = checkouts / target.checkout
        if not token or not cwd.is_dir():
            failures += 1
            emit(
                {
                    "action": "push",
                    "automation": target.automation,
                    "variable": target.variable,
                    "ok": False,
                    "error": "token file or checkout missing",
                }
            )
            continue
        if target.provider == "railway":
            ok = push_railway(run, cwd, target.service, target.variable, token)
        else:
            ok = push_vercel(run, cwd, target.variable, token)
            if ok and target.checkout not in vercel_checkouts:
                vercel_checkouts.append(target.checkout)
        failures += 0 if ok else 1
        emit(
            {
                "action": "push",
                "automation": target.automation,
                "provider": target.provider,
                "checkout": target.checkout,
                "service": target.service,
                "variable": target.variable,
                "ok": ok,
                "expires": time.strftime(
                    "%Y-%m-%d", time.gmtime(token_exp(token) or 0)
                ),
            }
        )
    for checkout in vercel_checkouts:
        ok = deploy_vercel(run, checkouts / checkout)
        failures += 0 if ok else 1
        emit({"action": "deploy", "provider": "vercel", "checkout": checkout, "ok": ok})
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="empire-token-rotate", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--tokens", type=Path, required=True)
    parser.add_argument("--key-dir", type=Path, required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--ttl-days", type=float, default=30.0)
    parser.add_argument(
        "--checkouts",
        type=Path,
        default=Path(os.environ.get("EMPIRE_CHECKOUTS_ROOT", str(REPO_ROOT.parent))),
    )
    parser.add_argument("--deployments", type=Path, default=DEPLOYMENTS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would rotate; mint and push nothing",
    )
    parser.add_argument(
        "--force", action="store_true", help="rotate every deployed automation now"
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="AUTOMATION",
        help="limit the run to these automations (repeatable)",
    )
    args = parser.parse_args(argv)

    targets, within = load_targets(args.deployments)
    if args.only:
        targets = [t for t in targets if t.automation in set(args.only)]
    due = due_automations(
        targets,
        args.tokens,
        args.checkouts,
        within,
        subprocess.run,
        time.time(),
        args.force,
    )
    if not args.only:
        registry = json.loads(
            (
                REPO_ROOT / "contracts" / "automations" / "v1" / "automations.json"
            ).read_text()
        )
        known = {item["id"] for item in registry["automations"]}
        due.update(local_due(args.tokens, known, within, time.time(), skip=set(due)))

    def mint(automations: list[str]) -> None:
        from empire_platform import automations as provisioning

        argv_ = [
            "provision",
            "--workspace",
            args.workspace,
            "--tokens",
            str(args.tokens),
            "--ttl-days",
            str(args.ttl_days),
            "--key-dir",
            str(args.key_dir),
            "--issuer",
            args.issuer,
        ]
        for name in automations:
            argv_ += ["--only", name]
        if provisioning.main(argv_) != 0:
            raise SystemExit("provisioning failed")

    failures = rotate(
        targets, due, args.tokens, args.checkouts, mint, subprocess.run, args.dry_run
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
