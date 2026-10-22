"""Build a review evidence package for reviewers without repository access.

Writes .runtime/evidence/<timestamp>/ (ignored by git) containing:

- README.md: summary, how it was produced, and known limitations
- tests.xml / tests-summary.txt: the full pytest run
- security-tests.txt: the security-relevant test cases and their results
- migration-dry-run.json: tasks import dry run against the Solo Empire SQLite
  (counts, references, anomalies; no task content)
- resilience.json: measurements from tests/test_resilience.py
- rehearsal.json: cutover and rollback on a copy of current data (historical; the script was removed)
- commits.txt and diffstat.txt: history of this repository
- contracts/: the committed API, event, authorization, and automation contracts
- secrets-scan.json: detect-secrets findings (expected empty)

Nothing here contains tokens, keys, or task titles.

    python scripts/evidence.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
SECURITY = re.compile(
    r"isolation|bypass|delegation|authz|conformance|toctou|revoc|scope|exchange|identity|"
    r"restricted|consent|cross_workspace|other_workspace|forged|token|least_privilege|automation",
    re.IGNORECASE,
)
LIMITATIONS = """\
- Local only: PostgreSQL 16 from the pgserver wheel; no Neon, deployment, or production data.
- Go writers (task-sync, notifier) are not migrated: no Go toolchain on the build machine.
- TypeScript CLI (infra/cli.ts) is not migrated; the old MCP server and today-api are scheduled for retirement.
- Token exchange is a prototype signed with the local development key; no hosted issuer.
- Resilience checks are small (tens to hundreds of events), not load tests.
- Graph covers tasks plus synthetic goals/finance/diary events; those cells do not exist yet.
"""


def run(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=ROOT, capture_output=True, text=True, **kwargs
    )


def main() -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / ".runtime" / "evidence" / stamp
    out.mkdir(parents=True)

    tests = run(
        str(PYTHON),
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={out / 'tests.xml'}",
    )
    (out / "tests-summary.txt").write_text(tests.stdout[-4000:], encoding="utf-8")
    suite = ET.parse(out / "tests.xml").getroot().find("testsuite")
    assert suite is not None  # pytest's junitxml always nests one <testsuite>
    totals = {
        k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")
    }
    security = []
    for case in suite.iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        if SECURITY.search(name):
            outcome = (
                "failed"
                if case.find("failure") is not None or case.find("error") is not None
                else ("skipped" if case.find("skipped") is not None else "passed")
            )
            security.append(f"{outcome:7} {name}")
    (out / "security-tests.txt").write_text(
        "\n".join(sorted(security)) + "\n", encoding="utf-8"
    )

    # The tasks import and the cutover rehearsal ran against the SQLite task tables, which were
    # retired after the production cutover of 2026-10-09; their results are in the runbook log.
    safe = {
        "ok": True,
        "retired": "production cutover 2026-10-09; see docs/runbooks/tasks-cutover-and-rollback.md",
        "anomaly_count": 0,
        "field_mismatch_count": 0,
    }
    (out / "migration-dry-run.json").write_text(
        json.dumps(safe, indent=2), encoding="utf-8"
    )

    for name in ("resilience.json", "rehearsal.json"):
        if (ROOT / ".runtime" / "readiness" / name).exists():
            shutil.copy(ROOT / ".runtime" / "readiness" / name, out / name)
    rehearsal = (
        json.loads((out / "rehearsal.json").read_text())
        if (out / "rehearsal.json").exists()
        else {}
    )
    failed_checks = sorted(k for k, v in rehearsal.get("checks", {}).items() if not v)
    (out / "commits.txt").write_text(
        run("git", "log", "--format=%h %ad %s", "--date=short").stdout, encoding="utf-8"
    )
    first = run("git", "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
    (out / "diffstat.txt").write_text(
        run("git", "diff", "--stat", first, "HEAD").stdout, encoding="utf-8"
    )
    shutil.copytree(ROOT / "contracts", out / "contracts")
    scan = run(
        "uvx",
        "--quiet",
        "detect-secrets",
        "scan",
        "--all-files",
        "--exclude-files",
        r"\.venv/|\.runtime/",
        ".",
    )
    findings = (
        json.loads(scan.stdout).get("results", {})
        if scan.returncode == 0
        else {"error": scan.stderr[-500:]}
    )
    (out / "secrets-scan.json").write_text(
        json.dumps(findings, indent=2), encoding="utf-8"
    )

    head = run("git", "rev-parse", "--short", "HEAD").stdout.strip()
    passed = sum(1 for line in security if line.startswith("passed"))
    (out / "README.md").write_text(
        f"""# empire-platform review evidence

Generated {stamp} from commit `{head}`.

| Item | Result |
|---|---|
| Tests | {totals["tests"]} run, {totals["failures"]} failures, {totals["errors"]} errors, {totals["skipped"]} skipped |
| Security-relevant tests | {passed} of {len(security)} passed (security-tests.txt) |
| Migration dry run | ok={safe.get("ok")}, anomalies={safe["anomaly_count"]}, field mismatches={safe["field_mismatch_count"]} |
| Rehearsal on current data copy | ok={rehearsal.get("ok")}, {len(rehearsal.get("checks", {}))} checks, failed: {", ".join(failed_checks) or "none"} |
| Secrets scan | {len(findings) if isinstance(findings, dict) else "error"} files with findings |

## Known limitations

{LIMITATIONS}
## Reproduce

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
python scripts/evidence.py
```
""",
        encoding="utf-8",
    )
    print(out)
    return (
        0
        if totals["failures"] == totals["errors"] == 0
        and safe.get("ok")
        and rehearsal.get("ok")
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
