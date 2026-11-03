# ADR-0005 — Platform-issued RS256 JWTs with delegations; 30-day automation tokens rotated weekly by a hosted cron

- Status: **accepted (retro-documented)** 2026-11-03
- Related central ADR (reused): architecture-docs [ADR-0003 hosted shared services](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0003-hosted-shared-services.md)

## Decision

- token = RS256 JWT issuer `empire-platform`; claims `sub`, `ws`, `del`/`obo` (คู่กัน), `act.sub`, `scope`, `aud` (`tasks-api`, `platform-api`, `graph-api`, และ audience ของ cell อื่น)
- token ไม่มี delegation (owner) ≤ 3600 s; token ที่มี delegation ≤ 30 วัน (hard cap) เพราะทุก call ตรวจ `revoked_at` และ expiry ของ delegation
- automation identities และ scopes อยู่ใน `contracts/automations/v1/automations.json` (46 ตัว, `delegation_days` 30); ที่ deploy อยู่ใน `deployments.json` (26 targets: Railway 17, Infisical 5, Vercel 2, Cloudflare 2; `rotate_within_days` 10)
- `token-rotate` Railway cron (`30 19 * * 0`) mint ใหม่และ push ผ่าน provider APIs; ค่า token อยู่ใน request body เท่านั้น ไม่ print
- JWKS ที่ `/.well-known/jwks.json` เผยแพร่ key ปัจจุบันและ `TASKS_API_JWT_NEXT_PUBLIC_KEY_PEM` (rollover)
- hosted mode ปฏิเสธ dev issuer / dev public key

## Evidence

`services/tasks/tasks_api/auth.py`, `services/tasks/tasks_api/app.py` (`server_config`, `_next_key`), `src/empire_platform/{automations,rotate,hosted_rotate,jwks,token_exchange}.py`, `contracts/automations/v1/*.json`, `tests/test_hosted_rotate.py`, `tests/test_rotate.py`, `tests/test_jwks.py`, `tests/test_hosted_deploy.py`, `docs/runbooks/cell-executions-and-tokens.md`

## Consequences

- signing private key อยู่ใน Railway variable ของ `token-rotate` และ Infisical; เคยหลุดเข้า transcript 2026-10-28 (ops record: infisical-list-names-safely (2026-11-03)) — การ rotate issuer key เป็น owner decision
- token เก่ายังใช้ได้จนถึง `exp`; ตัดทันทีต้อง revoke delegation
