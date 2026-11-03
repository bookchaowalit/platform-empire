# ADR-0004 — One Docker image for every service; Railway deploys from GitHub `main` after CI

- Status: **accepted (retro-documented)** 2026-11-03

## Decision

- `Dockerfile` เดียว (`python:3.12-slim`, `uv sync --frozen` จาก `uv.lock`, user `empire` uid 10001) และ `docker-entrypoint.sh` เลือก process จาก `EMPIRE_SERVICE` (`api`, `publisher`, `mcp`, `recurring`, `rotate`, `backlog-sync`, `stripe-reconcile`)
- Railway IaC `.railway/railway.ts` (partial `empire-platform`, `variables: { managed: false }`) ประกาศ 6 services; ทุกตัว `source: github` `bookchaowalit/platform-empire` `main` `checkSuites: true` (Wait for CI, ตั้งแต่ 2026-10-18)
- ห้าม `railway up` จาก working tree (`README.md`, `PRODUCT.md`)
- rollback = redeploy deployment ก่อนหน้าใน Railway

## Evidence

`Dockerfile`, `docker-entrypoint.sh`, `.railway/railway.ts`, `README.md` (Token rotation), `PRODUCT.md` (Deploys), PR #11 `docs/cd-github-wait-for-ci`, PR #12 `docs/cd-all-services`

## Consequences

- ใน runbook: `backlog-sync` และ `stripe-reconcile` ถูก deploy ด้วย `railway up` ของ `git archive` เพราะ `serviceConnect` ถูกปฏิเสธ (ไม่ redeploy ตอน push) — ขัดกับ IaC ที่ประกาศ `source: github`; สถานะปัจจุบันต้องยืนยัน
- ตั้งแต่ 2026-10-28 deployment จาก GitHub หยุดที่ `NEEDS_APPROVAL` ต้อง approve ผ่าน Railway GraphQL (ops record: railway-deploy-approval-gate (2026-11-03))
