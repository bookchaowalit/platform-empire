# 07 — C4 Component (tasks-api process)

```mermaid
C4Component
  title Components inside the tasks-api process
  Container_Boundary(api, "tasks-api") {
    Component(verifier, "TokenVerifier", "tasks_api.auth", "RS256, aud tasks-api, platform-api or graph-api, lifetime caps, MCP issuer")
    Component(routes, "Tasks routes", "tasks_api.app create_app", "tasks, links, assignments, dependencies, role suggestions, types, occurrences")
    Component(acting, "Write pipeline", "precheck and acting", "scope check, recorded decision, write switch, execution, enforce, emit, audit")
    Component(authz, "empire_authz client", "packages/authz", "authorize and enforce call platform.authorize")
    Component(plat, "Platform HTTP routes", "platform_api.routes register", "authz decide, executions, finish, graph events")
    Component(shadow, "IdentityShadow", "platform_api.identity_shadow", "Background mirror of decide to platform-identity")
    Component(jwks, "JWKS route", "empire_platform.jwks", "/.well-known/jwks.json, current and next key")
    Component(sentry, "Sentry init", "empire_platform._sentry", "errors only, scrubbed")
  }
  ContainerDb(db, "Neon PostgreSQL", "platform, tasks, graph")
  System_Ext(idn, "platform-identity")

  Rel(routes, verifier, "principal")
  Rel(routes, acting, "writes")
  Rel(acting, authz, "authorize, enforce")
  Rel(authz, db, "SELECT platform.authorize")
  Rel(acting, db, "begin_execution, DML, outbox, record_audit")
  Rel(plat, verifier, "platform-api and graph-api audiences")
  Rel(plat, authz, "decide and enforce")
  Rel(plat, db, "begin_cell_execution, finish_cell_execution, ingest_cell_event")
  Rel(plat, shadow, "queue after answer")
  Rel(shadow, idn, "POST /v1/authz/decide")
```

Sources: `services/tasks/tasks_api/app.py`, `services/tasks/tasks_api/auth.py`, `packages/authz/empire_authz/__init__.py`, `services/platform/platform_api/routes.py`, `services/platform/platform_api/identity_shadow.py`, `src/empire_platform/jwks.py`, `src/empire_platform/_sentry.py`, `src/empire_platform/__init__.py`, migrations `0006_cell_executions.sql`, `2004_cell_ingest.sql`

## Components ของ jobs (`src/empire_platform/`)

```mermaid
flowchart LR
  subgraph jobs["empire_platform package"]
    REC["recurring.py"]
    BL["backlog_sync.py"]
    SR["stripe_reconcile.py"]
    HR["hosted_rotate.py"]
    RO["rotate.py"]
    AU["automations.py and permissions.py"]
    MO["monitor.py"]
    CE["cells.py"]
    NE["neon.py"]
    DD["devdb.py, migration runner"]
    SE["_sentry.py"]
  end
  HR --> RO
  HR --> AU
  RO --> AU
  CE --> DD
  NE --> DD
  MO --> DD
  REC --> SE
  BL --> SE
  SR --> SE
```

Sources: imports ใน `src/empire_platform/hosted_rotate.py` (`from empire_platform.rotate import ...`), `rotate.py`, `cells.py`, `neon.py`, `monitor.py`, `recurring.py`, `backlog_sync.py`, `stripe_reconcile.py`

## Open questions

- ไม่มี
