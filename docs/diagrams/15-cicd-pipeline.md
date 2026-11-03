# 15 — CI/CD pipeline

```mermaid
flowchart TD
  T1(["push to main"]) --> CI["ci.yml, concurrency ci-ref, cancel in progress"]
  T2(["pull_request"]) --> CI
  T3(["workflow_dispatch"]) --> CI
  CI --> J1["platform: ci-python.yml v1.1.1, Python 3.12, ubuntu-24.04"]
  CI --> J2["security: security.yml v1.1.1"]
  J1 --> S1["Checkout, setup uv and Python, install"]
  S1 --> S2["Start PostgreSQL"]
  S2 --> S3["Lint: Ruff"]
  S3 --> S4["Typecheck: mypy"]
  S4 --> S5["Test: pytest"]
  S5 --> S6["Coverage gate 84 percent"]
  S6 --> S7["Drop CI database"]
  J2 --> G1["gitleaks"]
  J2 --> G2["semgrep"]
  J2 --> G3["osv-scanner"]
  J2 --> G4["trivy-fs"]
  S7 --> CHK{"All checks green on main"}
  G1 --> CHK
  G2 --> CHK
  G3 --> CHK
  G4 --> CHK
  CHK -->|"Railway Wait for CI, checkSuites true"| APP["Since 2026-10-28: deployment waits in NEEDS_APPROVAL"]
  APP --> D1["tasks-api"]
  APP --> D2["tasks-publisher"]
  APP --> D3["recurring-gen"]
  APP --> D4["token-rotate"]
  APP --> D5["backlog-sync and stripe-reconcile, per IaC"]
  CHK -->|"red"| KEEP["Production stays on previous deployment"]
```

Sources: `.github/workflows/ci.yml`, `bookchaowalit/platform-ci-workflows` `.github/workflows/ci-python.yml` และ `security.yml` @ `87dc5e1c` (job/step names via `gh api`), `.railway/railway.ts`, `README.md` (Token rotation), `Dockerfile`, ops record: railway-deploy-approval-gate (2026-11-03)

- CI ใช้ GitHub-hosted runner เพราะ repo เป็น public (comment ใน `ci.yml`)
- Coverage threshold: ratchet "measured 86.86% on 2026-10-22 (threshold = measured - 2)"
- Docker build เกิดใน Railway (ไม่มี image build/scan ใน GitHub Actions)
- Dependency updates: `renovate.json`

## Open questions

- `backlog-sync` และ `stripe-reconcile` เชื่อม GitHub source แล้วหรือยัง? runbooks (2026-10-24/25) บอกว่า deploy ด้วย `railway up` ของ `git archive` เพราะ `serviceConnect` ถูกปฏิเสธ แต่ IaC ประกาศ `source: github`
- ไม่มี post-deploy smoke ใน pipeline (verify อยู่ที่ healthcheck `/health` ของ Railway และ uptime monitor)
