# SPEC-002 — Catalog: shared domains, values and metric definitions

Status: proposed (2026-11-22, revised after two Codex review rounds the same day). Owner decision needed on the open
questions below before build.

## Problem

Every cell classifies its data its own way, so nothing can be read across cells:

- `life-goals-api` hard-codes `category` as an enum (`tech, business, life, creative, financial, health`)
  in `cell.yaml`; adding or renaming a category is a code change plus a migration.
- `life-time-api` has `domain` as free text; the check-in worker writes `work`, `learning`, `personal`.
- `life-roles-skills-api` has `category` as free text.
- No cell holds **values** (why a goal matters), and nothing **defines** a metric: cells store numbers
  (sleep 7, energy 3) but no unit, scale, threshold, target or kind (leading / lagging / guardrail).
- The domain list is not final and will change (new domains, merges, splits). Old data must not need rewriting.

Empire Graph already gives the shared structure: cell entities are nodes (`empire:<cell>:<type>:<id>`),
edges are typed by `graph.relation_types`, `graph.aliases` redirects URNs, and edges carry `classification`,
`provenance`, `valid_from`, `retracted_at`. What is missing is the shared **vocabulary** those nodes point at.

Today only `life-goals-api` emits graph events (`graph: [...]` in its `cell.yaml`); time, diary and health
do not. Metric sources therefore point at cell resources, not at graph nodes.

## Scope

The platform owns meaning (what a thing is, which domain, which value, how it is measured).
Cells keep their data, attributes and their own business logic. No cell business logic moves into the platform,
and the platform never stores measured values: values stay in cells and in the R2 lake.

In scope:

1. A concept registry for **domains** and **values**, workspace-scoped, versioned, merge/split without rewrites.
2. A **metric definition** catalog.
3. Four relation types so cells attach their entities to these concepts through the existing event ingest,
   plus the ingest changes those relations need (D5).
4. HTTP routes on the deployed `tasks-api`, MCP tools, and OpenAPI coverage.

Out of scope (separate specs): edge weights (one activity 70 % experience / 30 % finance), computing metrics
or evaluating criteria (lake Gold marts / `life-operating-cycles-api` do that), deploying the graph read API,
any UI.

## Decisions

### D1. Concepts are graph nodes owned by the `platform` cell, one set per workspace

`graph.nodes.urn` and `graph.aliases.alias_urn` are global primary keys (RLS hides rows but does not
partition uniqueness), so a concept URN must be unique across workspaces:

- Canonical URN: `empire:platform:<kind>:<workspace_id>:<slug>` with `kind` in `domain`, `value`, `metric`
  (metric uses its `key` as slug). It matches the existing `graph.nodes.urn` check.
- Cells may send the **short form** `empire:platform:<kind>:<slug>` as `target_ref`; ingest canonicalises it
  with the event's `workspace_id` before `graph.resolve()` (D5). Cells never need to know workspace ids in URNs.
- The slug/key is the **stable id**; the label may change freely.
- A concept write upserts its registry row (D2/D6) and its `graph.nodes` row (`cell = 'platform'`,
  `classification = 'internal'`) in one transaction, so edges resolve to a real node.

### D2. Registry table `graph.concepts`

| column | type | notes |
|---|---|---|
| `urn` | text PK | canonical form of D1 |
| `workspace_id` | uuid | RLS `workspace_isolation`, same as other graph tables |
| `kind` | text | `domain` \| `value` |
| `slug` | text | `^[a-z][a-z0-9-]{1,40}$`, unique per (workspace, kind) |
| `label` | text | display name, any language |
| `description` | text | what belongs here and what does not |
| `parent_urn` | text null | same kind only; domains may nest, max depth 3 |
| `status` | text | `active` \| `merged` \| `split` \| `retired` |
| `replaced_by` | text[] | required for `merged` (one URN) and `split` (two or more); targets must be `active` |
| `introduced_in` | int | taxonomy version that added it |
| `retired_in` | int null | taxonomy version that ended it (merge, split or retire) |
| `updated_at` | timestamptz | |

`graph.taxonomy_versions (workspace_id, version, note, created_by, created_at, unclassified_snapshot jsonb)`:
every concept write that creates a concept or changes `status` or `parent_urn` bumps the workspace version by one
with a required `note` (why it changed). Label/description edits do not bump it. `unclassified_snapshot`
(D4) is written in the same transaction.

Revisions: `graph.concept_revisions` and `graph.metric_revisions` are append-only (same RLS), one row per write
with the full new row, the caller, the execution id and the taxonomy version. Platform audit
(`platform.record_audit`) keeps its existing contract (`before_hash` / `after_hash`), with the revision id in the
audit event so the full change is recoverable from the revision table.

### D3. Change without rewriting old data

- **Rename:** update `label`. URN unchanged, no edge touched.
- **Serialisation:** every taxonomy write (create, merge, split, retire, parent change) first takes a per-workspace
  transaction lock (`pg_advisory_xact_lock` on the workspace's taxonomy key), then re-reads the status of every
  concept it touches. Two merges started together (A → B and B → A) therefore run one after the other, and the
  second sees its target `merged` and fails with 409; no alias cycle can be written.
- **Merge A into B:** B must be `active`. A gets `status = merged`, `replaced_by = {B}`, and a
  `graph.aliases` row A → B. In the same transaction every existing alias whose target is A is repointed to B,
  so aliases are always one hop and `graph.resolve()` (single lookup) stays correct after chained merges
  (A → B, then B → C leaves A → C and B → C). Because merge targets must be `active` (not `merged`), no cycle can form.
  Existing edges stay as stored (history kept); readers resolve them.
- **Split A into B, C:** targets must be `active`. A gets `status = split`, `replaced_by = {B, C}`. No alias
  (the right target is ambiguous). Existing edges to A stay valid and are listed by `GET /v1/catalog/review`
  until a cell or the owner re-links them.
- **Retire:** `status = retired`. Existing edges stay.
- After merge, new links to A land on B through the alias. After split or retire, new `in-domain` links to A fall
  back to `unclassified` with A recorded as requested; other catalog relations to A are rejected (D5).
- **Metrics that reference a domain** (`domain_urn`): on merge they move to B in the same transaction
  (each metric's `version` bumps). Split and retire are refused with 409 listing the affected metric keys unless
  the request carries `metric_moves: {<key>: <active domain slug>}` covering all of them, applied in the same
  transaction.
- Nothing is ever deleted from `graph.concepts`.

### D4. `unclassified` domain

Every workspace has the domain `unclassified`; it cannot be merged, split or retired. Cells link anything they
cannot place there.

`GET /v1/catalog/review` returns the **current** state: entities linked to `unclassified` and to `split` concepts,
grouped by cell/type with counts and recent labels. It is filtered **before** counting or returning labels with
the same rules as graph reads: an entity is included only when `graph.node_visible(urn, actor, delegation)` is true
for the caller, and edges of class `restricted` need a live consent (`graph.consents`) for the relation.
Entities the caller cannot see are not counted.

History per taxonomy version comes from `graph.taxonomy_versions.unclassified_snapshot`: when the version
bumps, the counts by cell/type (no labels, no URNs) are stored. Edge re-links clear `retracted_at` on the same
row (existing ingest), so edge rows alone cannot answer "was it unclassified in v2"; the snapshot is the record.

Snapshots count **only edges that need no consent** (classification `public`, `internal`, `confidential`);
`restricted` edges are never counted, because a consent can lapse after the snapshot and a stored total could not be
filtered afterwards. Counts are stored per cell/type/classification, and a read returns only the cell/types the
caller may read. The API response says `restricted_excluded: true` so the limit is visible.

### D5. Relation types and ingest changes

| relation | from | to | meaning |
|---|---|---|---|
| `in-domain` | `*` | `platform:domain` | entity belongs to this life domain (many allowed) |
| `serves-value` | `goals:goal`, `projects:project`, `habits:habit`, `platform:domain` | `platform:value` | why it matters |
| `measured-by` | `goals:goal`, `habits:habit`, `projects:project` | `platform:metric` | the metric that shows progress |
| `defined-by` | `operating:kpi` | `platform:metric` | an operating KPI uses this catalog definition (D7a) |

The migration also adds `operating:kpi` to `tracks-goal.from_types` (it lists `operating:metric`, a type the
operating cell does not emit).

Cells assert these with the existing `<cell>.<type>.linked` / `.unlinked` events. Ingest changes, in
`graph.ingest_event_v1` and the unlink path:

1. **Canonicalise** a short-form `target_ref` (D1) using the event's `workspace_id`, then `graph.resolve()`.
2. **Target check**, only for the four catalog relations above (targets `platform:domain`, `platform:value`,
   `platform:metric`; `assigned-to` → `platform:identity` and every other relation are unchanged): after resolve,
   the target must be a row in `graph.concepts` / `graph.metric_definitions` of this workspace with the right kind.
   - not found (typo, other workspace) → `rejected_relation`;
   - `active` → stored;
   - `split` or `retired` → for `in-domain` the edge is stored to `unclassified` with
     `provenance.requested = <original URN>` and outcome `fallback_unclassified`, so items created after a
     taxonomy change still show in `/v1/catalog/review` (which lists the requested concept) until the cell's
     mapping is updated; for `serves-value`, `measured-by`, `defined-by` → `rejected_relation`.
   A merged concept has already resolved to its active target.
3. **Unlink after merge:** the unlink path matches edges where `graph.resolve(to_urn)` equals the resolved target
   (still limited to `asserted_by` = the emitting cell), so an edge stored to A before A merged into B is retracted
   by an unlink that now resolves to B.
4. **Ordering by source version.** For the four catalog relations, `graph.edge_versions (workspace_id, from_urn,
   relation, asserted_by, version)` holds the highest subject `payload.version` applied, whether by `linked` or
   `unlinked`. A `linked` or `unlinked` event whose version is lower than that is recorded `stale` and changes no
   edge. So a late `linked(A)` after `unlinked(A)` + `linked(B)` does not bring A back. The backfill emits events
   with the source row's version read in the same transaction, so a concurrent edit (higher version) wins.

### D6. Metric definitions `graph.metric_definitions`

| column | type | notes |
|---|---|---|
| `urn` | text PK | canonical form of D1 (`kind = metric`, slug = key) |
| `workspace_id` | uuid | RLS |
| `key` | text | `^[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*$`, e.g. `health.sleep_hours`; stable; unique per workspace |
| `label`, `description` | text | the construct and its operational definition ("energy after work, 1 = empty, 5 = full, asked at 21:30") |
| `domain_urn` | text | must be an `active` domain of the same workspace (kept true by D3) |
| `unit` | text null | `h`, `kg`, `min`, `L`, `THB`, `score`… |
| `scale` | text | `nominal` \| `ordinal` \| `interval` \| `ratio` |
| `value_range` | jsonb null | bounds of one **raw** observation, e.g. `{"min": 1, "max": 5}` |
| `kind` | text | `leading` \| `lagging` \| `guardrail` \| `descriptive` |
| `objectivity` | text | `objective` \| `subjective` |
| `source` | jsonb | `{"cell", "resource", "field", "filter", "time_field", "read_permission", "dedupe"}`: where one raw observation is read; `read_permission` is the cell permission that reads it (e.g. `diary.journal-entry:read`); `dedupe` picks one source per period when several write the same metric (`{"by": "day", "field": "source", "prefer": ["apple-health", "telegram-checkin"]}`) |
| `aggregation` | text | how a period is summarised, see D7 |
| `cadence` | text | `daily` \| `weekly` \| `monthly` \| `event` |
| `criteria` | jsonb | ordered list `[{"level": "warn", "window": "7d", "op": "<", "value": 6, "action": "…"}]`, compared to the **aggregate** |
| `target` | jsonb null | `{"window": "7d", "op": ">=", "value": 7}`; null is allowed (a KPI need not have a target) |
| `classification` | text | defaults to the source resource's classification |
| `status` | text | `active` \| `retired` (+ `replaced_by` text null) |
| `version` | int | bumps on any change to scale, aggregation, source, domain, criteria or target |

### D7. Validation the catalog enforces

- `aggregation` must fit `scale`: nominal → `count`, `mode`, `last`; ordinal → adds `median`, `min`, `max`;
  interval → adds `mean`; ratio → adds `sum`. So `diary.energy` (1–5, ordinal) cannot be averaged.
  `last` needs `source.time_field` (the field the latest observation is chosen by).
- `criteria` / `target` comparisons (`<`, `>=`…) are refused for nominal metrics.
- Thresholds are checked against the range of the **aggregate**, not the raw value:
  for `mean`, `median`, `min`, `max`, `last`, `mode` the aggregate range is `value_range`;
  for `sum` and `count` the number of observations in a window is not known from the definition, so only the sign
  is guaranteed: `sum` is `[0, unbounded)` when `value_range.min ≥ 0`, otherwise unbounded; `count` is `[0, unbounded)`.
  So a weekly water target of 14 L passes with `value_range.max = 10` per observation, and a `sum` threshold of 40
  passes with `value_range.min = 20`.
- `source.cell` must be a known cell; the catalog does not read the cell, it only stores the pointer.
- `dedupe` is required when the `source.filter` can match more than one writer of the same value (the check-in
  and Apple Health both write `sleep_hours`, `exercise_minutes` and `weight` to `health-metrics`).
- `read_permission` is required when `classification` is `confidential` or `restricted`.
- Every write is audited per D2 (hash audit + full revision row).

### D7b. Who may read a metric definition

A definition carries owner-written text (description, criteria actions, filters) about its source. Reads through
the API, MCP and the lake export follow the source's classification:

- `public` / `internal`: `catalog.metric-definition:read` is enough;
- `confidential` / `restricted`: the caller also needs `source.read_permission`; otherwise the definition is left out
  of lists and `GET` returns 404;
- the lake export (rollout step 5) contains only `public` and `internal` definitions.

### D7a. Relation to the operating cell's `kpi`

`life-operating-cycles-api` already has `kpi` (slug, unit, direction, target_value, source_kind, cadence) and
`kpi-measurement`. They stay: that cell owns its KPIs for its own sources (`cycle_tasks`, `ai_telemetry`,
`review`, `manual`) and their measured values. The catalog does not copy them. Overlap is handled by linking:

- an operating KPI that reads another cell's data (e.g. weekly sleep) links `defined-by` → `platform:metric`,
  and the catalog definition is the one that holds scale, aggregation and criteria;
- a KPI that only makes sense in the operating cell (cycle completion, AI telemetry) needs no catalog entry.

### D8. API, database access and permissions

Routes run inside `tasks-api` on the `tasks_api` database role (platform routes are registered with that
connection in `main()`), so the catalog follows the `graph.ingest_cell_event` pattern: `SECURITY DEFINER`
functions `graph.catalog_*` (list, upsert, merge, split, retire, review, versions, metric get/put) owned by the
migrator, `EXECUTE` granted to `tasks_api` only, each bound to `platform.current_workspace()`. `tasks_api` gets no
direct table grants; `graph_api` gets `SELECT` for the future graph read API.

Authorization: reads check `platform.can(actor, <permission>, delegation)`. **Writes** call
`platform.authorize(actor, workspace, <permission>, 'catalog.concept' | 'catalog.metric-definition', <kind/slug or key>,
workspace, delegation, on_behalf_of, approval, 'api', p_lock => true)` inside the writing transaction, so workspace
policies apply: when a policy marks the permission `requires_approval`, the write is refused with the platform's
usual approval-required response unless the request carries a granted `approval_id`. No new approval flow is added.

Permissions for owners:
- existing workspaces: the migration adds `catalog.concept:read|write|admin` and
  `catalog.metric-definition:read|write` to each workspace's `owner` role (`platform.role_permissions`);
- new workspaces: the role map that `platform-identity` passes to `platform.tenancy_signup` as `p_roles` gets the
  same permissions for `owner` (a change in the `platform-identity` repository, rollout step 1b).

Concepts are addressed by `{kind}/{slug}` (domain and value may share a slug):

| route | permission |
|---|---|
| `GET /v1/catalog/concepts?kind=&status=` | `catalog.concept:read` |
| `POST /v1/catalog/concepts` (create) | `catalog.concept:write` |
| `PATCH /v1/catalog/concepts/{kind}/{slug}` (label, description, parent) | `catalog.concept:write` |
| `POST /v1/catalog/concepts/{kind}/{slug}/merge` `{into, note}` | `catalog.concept:admin` |
| `POST /v1/catalog/concepts/{kind}/{slug}/split` `{into: [..], note, metric_moves?}` | `catalog.concept:admin` |
| `POST /v1/catalog/concepts/{kind}/{slug}/retire` `{note, metric_moves?}` | `catalog.concept:admin` |
| `GET /v1/catalog/metrics?domain=&kind=` | `catalog.metric-definition:read` |
| `PUT /v1/catalog/metrics/{key}` | `catalog.metric-definition:write` |
| `GET /v1/catalog/review` | `catalog.concept:read` + visibility filter (D4) |
| `GET /v1/catalog/versions` | `catalog.concept:read` |

Concept and metric reads show merged URNs as their target with `was` listing the old URNs.
MCP: `catalog.concepts`, `catalog.metrics`, `catalog.review` (read only).

**OpenAPI:** today `empire-contracts --check` builds the schema from `create_app()` only, while platform routes are
added later in `main()`, so platform routes are not in the contract. Catalog routes are registered by one function
that both `main()` and `contracts.py` call, and the contract file includes them; `empire-contracts --check` fails
when a catalog route is missing or changed.

## Seed (taxonomy version 1)

Seeding is `graph.catalog_seed()`, `SECURITY DEFINER`, idempotent (re-running inserts nothing), and it seeds only
`platform.current_workspace()` (refuses when unbound). Callers:

- **New workspaces:** `platform.tenancy_signup` (runs as `platform_tenancy`) sets `app.workspace_id` to the new
  workspace with `set_config(..., true)` and calls the seed in the same transaction. `platform_tenancy` gets
  `USAGE` on schema `graph` and `EXECUTE` on `graph.catalog_seed()` only; no catalog table privilege.
- **Existing workspaces (migration):** `platform.workspaces` has FORCE RLS, so an unbound migrator sees no rows
  (the bug fixed by `0011_signup_identity_backfill.sql`). The migration follows that pattern: inside its single
  transaction it lifts FORCE on `platform.workspaces`, reads the workspace ids, restores FORCE, then for each id sets
  `app.workspace_id` locally and calls `graph.catalog_seed()`.

Domains (owner may rename before build; slugs are the stable part):

| slug | label |
|---|---|
| `health` | สุขภาพและพลังงาน |
| `finance` | การเงินและทรัพยากร |
| `skills` | ความรู้และทักษะ |
| `work` | งานและการสร้างคุณค่า |
| `relationships` | ความสัมพันธ์และเสน่ห์ |
| `experience` | ประสบการณ์และการผจญภัย |
| `aesthetics-rest` | สุนทรียะ ความสุข และความชิล |
| `meaning` | ความหมายและการเติบโต |
| `unclassified` | ยังไม่จัดหมวด |

Values: none seeded; the owner writes them (open question Q2).

Metrics, from what the evening check-in writes today (`life-checkin-worker/src/worker.ts`, `parse.ts`).
Criteria and targets marked **(proposed)** need owner OK:

| key | source (cell / resource / field, filter, time field) | scale / unit, value_range | kind | aggregation | criteria / target |
|---|---|---|---|---|---|
| `health.sleep_hours` | health / `health-metrics` / `value`, `metric_type = sleep_hours`, `recorded_at`, dedupe by day prefer `apple-health` then `telegram-checkin` | ratio h, 0–24 | leading | mean 7d | warn mean 7d < 6; target ≥ 7 (proposed) |
| `health.weight_kg` | health / `health-metrics` / `value`, `metric_type = weight`, `recorded_at`, dedupe by day prefer `apple-health` then `telegram-checkin` | ratio kg, 20–300 | lagging | last | none |
| `health.exercise_minutes` | health / `health-metrics` / `value`, `metric_type = exercise_minutes`, `recorded_at`, dedupe by day prefer `apple-health` (daily total) then sum of `telegram-checkin` rows | ratio min, ≥ 0 | leading | sum 7d | target ≥ 150 / week (proposed) |
| `health.water_litres` | health / `health-metrics` / `value`, `metric_type = water`, `recorded_at` | ratio L, 0–10 | leading | mean 7d | target ≥ 2 (proposed) |
| `diary.mood` | diary / `journal-entries` / `mood`, `entry_type = daily`, `entry_date` | ordinal 1–5 | lagging | median 7d | warn median 7d ≤ 2 (proposed) |
| `diary.energy` | diary / `journal-entries` / `energy_level`, `entry_type = daily`, `entry_date` | ordinal 1–5 | guardrail | median 7d | warn median 7d < 3 → plan rest (proposed) |
| `time.work_minutes` | time / `time-logs` / `minutes`, `domain = work`, `log_date` | ratio min, ≥ 0 | guardrail | sum 7d | warn > 3,600 / week (60 h) (proposed) |
| `time.learning_minutes` | time / `time-logs` / `minutes`, `domain = learning`, `log_date` | ratio min, ≥ 0 | leading | sum 7d | none until owner sets one |

Category mapping (cells emit `in-domain` edges; the goals enum stays until a later goals spec):
goals `tech → skills`, `business → work`, `life → unclassified`, `creative → aesthetics-rest`,
`financial → finance`, `health → health`. Time logs `work → work`, `learning → skills`, `personal → unclassified`.

## Rollout

1. Migration `services/graph/migrations/2006_catalog.sql`: tables, RLS, revision tables, relation types,
   `graph.catalog_*` functions and grants (D8), ingest changes (D5), `graph.edge_versions`, owner-role
   permissions for existing workspaces, `graph.catalog_seed` for every workspace, and the `platform.tenancy_signup`
   change. Applied with the migrator URI **before** the merge (cells and platform do not migrate on deploy).
1b. `platform-identity`: add the catalog permissions to the `owner` role map passed as `p_roles` (its own change and deploy).
2. Routes + MCP tools in `tasks-api`; shared route registration and contract (D8); `empire-contracts --check`.
3. **Producers**, so data written after the backfill is classified too:
   - `life-goals-api` (already emits graph events): emit `goals.goal.linked` `in-domain` from `category`
     on create and on category change, `unlinked` for the old one.
   - `life-time-api` (emits nothing today): enable graph events for `time-log` and emit `in-domain` from `domain`
     the same way. Enabling events only fills the cell outbox; delivery needs, all in that repository:
     a publisher token with `graph.time:write` set as `TIME_PUBLISHER_TOKEN` (with `EMPIRE_PLATFORM_URL`) on
     `time-api` for inline publish, and an outbox sweep (a cron service in `.railway/railway.ts`, like
     `tasks-publisher`) that re-sends rows whose inline publish failed. Minting the token is an owner action.
     The check-in worker needs no change: it posts to the time cell, and the cell emits.
   - Each producer's category/domain → slug mapping lives in that cell; after a split or retire the owner updates it
     (until then new items fall back to `unclassified`, D5).
   Each is its own change in that cell's repository, merged after step 1.
4. Backfill job: one `in-domain` edge per existing goal and time log from the mapping above (idempotent).
5. Daily export of `graph.concepts` + `graph.metric_definitions` to the R2 lake so Gold marts join definitions
   (reuse the `data-schedules` cron project; which job carries it is decided at build time).
6. Follow-ups, each its own spec: goals `category` enum → domain reference; check-in metrics evaluated against
   criteria in `life-operating-cycles-api` weekly review.

## Acceptance (tests)

- AC-1 create a domain → registry row + `graph.nodes` row with `cell = platform` and canonical URN; taxonomy version +1 with its note; one revision row; audit event carries the revision id.
- AC-2 rename a domain → URN and edges unchanged; taxonomy version unchanged; revision row written.
- AC-3 (database level, the graph read API is out of scope) merge A into B → `graph.aliases` A → B; a new `in-domain` edge to A is stored to B; the catalog read function returns B with `was = [A]` for an edge stored to A.
- AC-4 merge A into B, then B into C → `graph.resolve(A) = C` and `graph.resolve(B) = C`; merging into a non-active concept → 409.
- AC-5 an edge to A stored before A merged into B is retracted by an `unlinked` event that targets A or B from the same cell; an unlink from another cell retracts nothing.
- AC-6 split A into B, C → no alias; old edges to A listed by `/v1/catalog/review`; a new `in-domain` link to A → `fallback_unclassified`; a new `serves-value` link to a split value → `rejected_relation`.
- AC-7 link to a non-existent slug (typo) or to another workspace's concept → `rejected_relation`; `in-domain` to a retired domain → `fallback_unclassified`; existing edges kept.
- AC-8 short-form `target_ref` `empire:platform:domain:health` is stored as the canonical URN of the event's workspace.
- AC-9 two workspaces both seeded: each has its own `unclassified` and `health`; workspace B cannot read or write A's concepts, metrics or revisions (RLS + D8 functions).
- AC-10 workspace created by `platform.tenancy_signup` after the migration has taxonomy version 1 with all seeded domains and metrics; running `graph.catalog_seed` again inserts nothing.
- AC-11 `unclassified` refuses merge, split and retire (409).
- AC-12 retire or split of a domain used by a metric → 409 naming the metric keys; with full `metric_moves` → 200 and metrics moved with `version` +1; merge moves metrics to the target.
- AC-13 domain and value with the same slug: `PATCH /v1/catalog/concepts/value/health` changes only the value.
- AC-14 `serves-value` from `tasks:task` → `rejected_relation` (type not allowed by D5).
- AC-15 `PUT` metric with `scale = ordinal`, `aggregation = mean` → 422; with `median` → 200; `last` without `source.time_field` → 422; every seeded metric passes `PUT` unchanged.
- AC-16 nominal metric with `<` criterion → 422; ratio metric with `value_range.max = 10`, `sum` 7d and target ≥ 14 → 200; `mean` with target 14 → 422.
- AC-17 metric change to criteria bumps its `version`; label-only change does not.
- AC-18 caller with `catalog.concept:read` but without read permission on the source cell type sees neither labels nor counts of those entities in `/v1/catalog/review` or version snapshots; restricted edges without consent are excluded.
- AC-19 version bump writes `unclassified_snapshot` with counts by cell/type only (no labels, no URNs).
- AC-20 caller without `catalog.concept:admin` gets 403 on merge/split/retire; `tasks_api` has no direct table privilege on catalog tables (only `EXECUTE` on `graph.catalog_*`).
- AC-21 `empire-contracts --check` fails when a catalog route is removed or its schema changes.
- AC-22 a goal created through the goals cell API after rollout gets an `in-domain` edge from its category; a time log created through the time cell API (as the check-in worker does) gets one from its `domain`.
- AC-23 backfill run twice → second run asserts no new edges.
- AC-24 `operating:kpi` → `platform:metric` with `defined-by` is stored; `tracks-goal` from `operating:kpi` is accepted.
- AC-25 a workspace policy with `requires_approval` on `catalog.concept:admin`: merge by a caller holding the permission but no granted approval is refused; with a granted `approval_id` it succeeds.
- AC-26 signup through `platform.tenancy_signup` as `platform_tenancy` succeeds and seeds the new workspace; `platform_tenancy` cannot select from or write to catalog tables directly; `graph.catalog_seed()` unbound raises.
- AC-27 a workspace created before the migration is seeded by the migration (test creates it first, then migrates).
- AC-28 the owner of an existing workspace and of a newly signed-up workspace can call `POST /v1/catalog/concepts` and the merge route (200), not only be refused in negative cases.
- AC-29 merge A → B and merge B → A started concurrently: one succeeds, the other gets 409; `graph.resolve` of both ends at an active concept.
- AC-30 `assigned-to` from `tasks:task` to `platform:identity` is accepted exactly as before the migration.
- AC-31 goal category changed A → B, then a delayed `linked(A)` with the older version arrives → `stale`, no edge to A; a backfill event read before a concurrent edit is `stale` after the edit's event.
- AC-32 after `work` is split, a goal created with category `business` gets an `in-domain` edge to `unclassified` with `provenance.requested` = the `work` URN, and `/v1/catalog/review` lists it under `work`.
- AC-33 snapshot with a `restricted` edge in `unclassified`: the edge is not counted; the response has `restricted_excluded: true`.
- AC-34 a caller with `catalog.metric-definition:read` but without `diary.journal-entry:read` gets 404 for `diary.energy` and does not see it in lists; the lake export contains no `confidential`/`restricted` definition.
- AC-35 a day with both an Apple Health and a check-in `exercise_minutes` row counts only the Apple Health value in `sum 7d`; a day with only check-in rows sums them.
- AC-36 a time log created through the time cell API while the platform is unreachable reaches the graph after the outbox sweep runs (AC-22 on retry).

## Open questions (owner)

- Q1 Domain labels and the 8-domain seed: keep, rename or change before build?
- Q2 Values: which values to seed (e.g. อิสรภาพ, สุขภาพ, ความรัก, การเติบโต, ความงาม)?
- Q3 Proposed criteria and targets in the metric seed: accept, change or leave empty?
- Q4 Relationship data (people) has no cell yet; domain `relationships` will stay empty until one exists. Build a relationships cell (restricted), or keep it in diary text?
- Q5 Deploy the graph read API (tech-debt item) now, or keep catalog reads on `tasks-api` only?
- Q6 Operating KPIs (D7a): link by `defined-by` as proposed, or move all cross-cell KPI definitions into the catalog
  and keep only measurements in the operating cell?
