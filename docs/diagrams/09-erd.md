# 09 — ERD

ข้อมูลถาวรทั้งหมดอยู่ใน PostgreSQL database `empire` (Neon project `empire-platform`) สาม schema; ทุกตารางที่มี `workspace_id` มี forced RLS ภาพนี้แสดงคอลัมน์หลัก (ไม่ใช่ทุกคอลัมน์) จาก `CREATE TABLE` และ `ALTER TABLE ... ADD COLUMN`

## Schema `platform`

```mermaid
erDiagram
  identities {
    uuid id PK
    text kind "human, agent, service_account, automation"
    text display_name
    text external_subject UK
    text status "active, disabled"
  }
  workspaces {
    uuid id PK
    text slug UK
    text name
    uuid owner_identity_id FK
    text status "active, suspended, closed"
  }
  memberships {
    uuid workspace_id PK
    uuid identity_id PK
    text status "active, suspended"
  }
  roles {
    uuid id PK
    uuid workspace_id FK
    text name
  }
  role_permissions {
    uuid workspace_id FK
    uuid role_id FK
    text permission "cell.resource:verb"
  }
  membership_roles {
    uuid workspace_id FK
    uuid identity_id FK
    uuid role_id FK
  }
  policies {
    uuid id PK
    uuid workspace_id FK
    text permission "unique per workspace"
    boolean requires_approval
    jsonb limits
  }
  delegations {
    uuid id PK
    uuid workspace_id FK
    uuid grantor_identity_id FK
    uuid grantee_identity_id FK
    text_array scopes
    timestamptz expires_at
    timestamptz revoked_at
    bigint budget_tokens
    numeric budget_thb
  }
  approvals {
    uuid id PK
    uuid workspace_id FK
    text requested_action
    uuid approver_identity_id FK
    text decision "pending, approved, rejected"
    timestamptz decided_at
  }
  executions {
    uuid id PK
    uuid workspace_id FK
    uuid initiated_by FK
    uuid executed_by FK
    uuid on_behalf_of FK
    uuid delegation_id FK
    uuid approval_id FK
    text channel "api, mcp, workflow, cli, event"
    text idempotency_key
    text status "running, succeeded, failed, cancelled, awaiting_approval"
    text correlation_id
  }
  cell_executions {
    uuid execution_id PK
    uuid workspace_id FK
    text service
    text permission
    text resource_type
    text resource_id
  }
  audit_events {
    uuid id PK
    uuid workspace_id FK
    uuid execution_id FK
    text service
    text action
    text before_hash
    text after_hash
  }
  authorization_decisions {
    uuid id PK
    uuid workspace_id FK
    uuid actor_identity_id
    text action
    text decision "allow, deny, require_approval"
    text reason
    boolean locked
  }
  identities ||--o{ workspaces : owns
  workspaces ||--o{ memberships : has
  identities ||--o{ memberships : joins
  workspaces ||--o{ roles : defines
  roles ||--o{ role_permissions : grants
  memberships ||--o{ membership_roles : holds
  roles ||--o{ membership_roles : assigned
  workspaces ||--o{ policies : sets
  identities ||--o{ delegations : grantor
  identities ||--o{ delegations : grantee
  workspaces ||--o{ approvals : has
  workspaces ||--o{ executions : runs
  delegations |o--o{ executions : authorizes
  approvals |o--o{ executions : approves
  executions ||--o| cell_executions : binds
  executions ||--o{ audit_events : records
  workspaces ||--o{ authorization_decisions : logs
```

Sources: `services/platform/migrations/0001_platform_core.sql`, `0003_authorization_decision_point.sql`, `0005_correlation.sql`, `0006_cell_executions.sql`

## Schema `tasks`

```mermaid
erDiagram
  tasks {
    uuid id PK
    uuid workspace_id
    bigint legacy_id "task number, NOT NULL since 1005"
    text title
    text status "todo, in_progress, done, blocked, cancelled"
    smallint priority "1 to 5"
    text external_source
    text external_id
    text recurrence_rule
    uuid parent_recurring_id FK
    uuid parent_task_id FK
    timestamptz archived_at
    integer version
    uuid created_by_execution
    uuid updated_by_execution
  }
  task_types {
    uuid id PK
    uuid workspace_id
    text name
    text default_status
  }
  task_dependencies {
    uuid id PK
    uuid task_id FK
    uuid depends_on_task_id FK
    text dependency_type
  }
  task_comments {
    uuid id PK
    uuid task_id FK
    text comment_type
    text content
  }
  task_attachments {
    uuid id PK
    uuid task_id FK
    text file_ref "artifact manifest reference"
  }
  task_assignments {
    uuid id PK
    uuid task_id FK
    text assignee_type "identity, role, team"
    text assignee_ref
    text assignment_type "assigned, cc, viewer"
  }
  task_links {
    uuid id PK
    uuid task_id FK
    text relation
    text target_type
    text target_ref "empire URN"
  }
  task_role_suggestions {
    uuid id PK
    uuid task_id FK
    text role_id
    text confidence
    text review_status "suggested, accepted, dismissed"
  }
  reminder_deliveries {
    uuid id PK
    uuid task_id FK
    timestamptz reminder_at
    text channel
    uuid execution_id
  }
  outbox {
    uuid id PK
    uuid workspace_id
    text type "tasks.entity.verb"
    text subject
    uuid execution_id
    text classification
    jsonb payload
    timestamptz published_at
    integer attempts
    timestamptz next_attempt_at
    text last_error
    text correlation_id
  }
  settings {
    text key PK "write switch"
    text value
  }
  workspace_counters {
    uuid workspace_id PK
    bigint next_number
  }
  tasks ||--o{ tasks : "parent or recurring parent"
  tasks ||--o{ task_dependencies : has
  tasks ||--o{ task_comments : has
  tasks ||--o{ task_attachments : has
  tasks ||--o{ task_assignments : has
  tasks ||--o{ task_links : has
  tasks ||--o{ task_role_suggestions : has
  tasks ||--o{ reminder_deliveries : has
```

Sources: `services/tasks/migrations/1001_tasks_core.sql`, `1002_writer_parity.sql`, `1004_write_switch.sql`, `1005_numbers_and_correlation.sql`

`tasks.*.workspace_id` และ `execution_id` ไม่มี FK ข้าม schema ไปที่ `platform` (อ้างด้วยค่าและ RLS เท่านั้น); FK ภายใน `tasks` เป็นแบบ composite `(workspace_id, id)`

## Schema `graph`

```mermaid
erDiagram
  relation_types {
    text relation PK
    text_array from_types
    text_array to_types
  }
  nodes {
    text urn PK "empire:cell:type:id"
    uuid workspace_id
    text cell
    text type
    text classification
    text lifecycle "active, archived, deleted"
    bigint version
  }
  aliases {
    text alias_urn PK
    uuid workspace_id
    text urn
  }
  edges {
    uuid id PK
    uuid workspace_id
    text from_urn
    text to_urn
    text relation FK
    text classification
    uuid execution_id
    timestamptz retracted_at
  }
  consents {
    uuid id PK
    uuid workspace_id
    uuid granted_by
    text relation FK
    text subject_urn
    timestamptz expires_at
    timestamptz revoked_at
  }
  inbox {
    text consumer PK
    uuid event_id PK
    uuid workspace_id
    text outcome
    text correlation_id
  }
  relation_types ||--o{ edges : types
  relation_types ||--o{ consents : scopes
  nodes ||--o{ aliases : "aliased by"
  nodes ||--o{ edges : "from or to"
```

Sources: `services/graph/migrations/2001_graph_core.sql`, `2002_correlation.sql`

ความสัมพันธ์ `nodes → aliases / edges` เป็นการอ้างด้วยค่า URN (ไม่มี FK ใน DDL) ภาพแสดงตามการใช้งานใน `graph.resolve`, `graph.neighbors`

ERD ระดับ workspace: architecture-docs [`docs/architecture/ERD-DIAGRAMS.md`](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/ERD-DIAGRAMS.md) (reused, กว้างกว่า)

## Open questions

- `delegations.budget_tokens` / `budget_thb` และ `policies.limits` มีใน schema แต่ไม่พบโค้ดที่บังคับ budget หรือ limits; จะใช้ใน Phase 2 หรือไม่
