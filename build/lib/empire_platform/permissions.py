"""Permission sets used by the platform's own tooling and tests.

Write access to tasks is split by operation so automations get only what they
use: ``tasks.task:write`` (create, upsert, general edits), ``tasks.status:write``
(status, due date, progress, complete), ``tasks.calendar:write``,
``tasks.link:write``, ``tasks.archive:write``, ``tasks.reminder:write``, and
``tasks.occurrence:write``, ``tasks.assignment:write`` (assign roles or people),
and ``tasks.suggestion:write`` (role suggestions and their review).
"""

DECISIONS_OWNER = (
    "decisions.decision:read",
    "decisions.decision:write",
    "decisions.decision:approve",
    "decisions.decision:delete",
    "decisions.review:write",
    "graph.decisions:write",
)

HABITS_OWNER = (
    "habits.habit:read",
    "habits.habit:write",
    "habits.habit:delete",
    "habits.routine:read",
    "habits.routine:write",
    "habits.routine:delete",
    "graph.habits:write",
)

NOTIFICATIONS_OWNER = (
    "notifications.rule:read",
    "notifications.rule:write",
    "notifications.notification:read",
    "notifications.notification:write",
)

GOALS_OWNER = (
    "goals.goal:read",
    "goals.goal:write",
    "goals.goal:delete",
    "goals.goal-task:read",
    "goals.goal-task:write",
    "goals.goal-task:delete",
    "goals.goal-metric:read",
    "goals.goal-metric:write",
    "goals.goal-metric:delete",
    "goals.goal-portfolio-mapping:read",
    "goals.goal-portfolio-mapping:write",
    "goals.goal-portfolio-mapping:delete",
    "goals.productivity-goal:read",
    "goals.productivity-goal:write",
    "goals.productivity-goal:delete",
    "goals.goal-project-link:read",
    "goals.goal-project-link:write",
    "goals.goal-project-link:delete",
    "goals.goal-context-link:read",
    "goals.goal-context-link:write",
    "goals.goal-context-link:delete",
    "goals.goal-task-project-link:read",
    "goals.goal-task-project-link:write",
    "goals.goal-task-project-link:delete",
    "graph.goals:write",
)

PROJECTS_OWNER = (
    "projects.project:read",
    "projects.project:write",
    "projects.project:delete",
    "projects.project-repo:read",
    "projects.project-repo:write",
    "projects.project-repo:delete",
    "projects.portfolio-context:read",
    "projects.portfolio-context:write",
    "projects.portfolio-context:delete",
    "projects.project-context-link:read",
    "projects.project-context-link:write",
    "projects.project-context-link:delete",
    "projects.work-item:read",
    "projects.work-item:write",
    "projects.work-item:delete",
    "projects.work-item-task-link:read",
    "projects.work-item-task-link:write",
    "projects.work-item-task-link:delete",
    "projects.work-item-goal-task-link:read",
    "projects.work-item-goal-task-link:write",
    "projects.work-item-goal-task-link:delete",
    "graph.projects:write",
)

ROLES_SKILLS_OWNER = (
    "roles-skills.personal-role:read",
    "roles-skills.personal-role:write",
    "roles-skills.personal-role:delete",
    "roles-skills.personal-role-context-link:read",
    "roles-skills.personal-role-context-link:write",
    "roles-skills.personal-role-context-link:delete",
    "roles-skills.personal-role-goal-link:read",
    "roles-skills.personal-role-goal-link:write",
    "roles-skills.personal-role-goal-link:delete",
    "roles-skills.personal-role-project-link:read",
    "roles-skills.personal-role-project-link:write",
    "roles-skills.personal-role-project-link:delete",
    "roles-skills.personal-role-task-link:read",
    "roles-skills.personal-role-task-link:write",
    "roles-skills.personal-role-task-link:delete",
    "roles-skills.role-goal-link:read",
    "roles-skills.role-goal-link:write",
    "roles-skills.role-goal-link:delete",
    "roles-skills.skill-category:read",
    "roles-skills.skill-category:write",
    "roles-skills.skill-category:delete",
    "roles-skills.skill:read",
    "roles-skills.skill:write",
    "roles-skills.skill:delete",
    "roles-skills.role-path-step:read",
    "roles-skills.role-path-step:write",
    "roles-skills.role-path-step:delete",
    "roles-skills.role-project-allocation:read",
    "roles-skills.role-project-allocation:write",
    "roles-skills.role-project-allocation:delete",
    "roles-skills.role-skill-link:read",
    "roles-skills.role-skill-link:write",
    "roles-skills.role-skill-link:delete",
    "roles-skills.progress-entry:read",
    "roles-skills.progress-entry:write",
    "roles-skills.progress-entry:delete",
    "graph.roles-skills:write",
)

ACCOUNTS_OWNER = tuple(
    f"accounts.{resource}:{right}"
    for resource in ("identity", "domain", "mailbox", "account")
    for right in ("read", "write", "delete")
)

OPERATING_OWNER = (
    "operating.cycle:read",
    "operating.cycle:write",
    "operating.cycle:delete",
    "operating.cycle-task:read",
    "operating.cycle-task:write",
    "operating.cycle-task:delete",
    "operating.cycle-goal-link:read",
    "operating.cycle-goal-link:write",
    "operating.cycle-goal-link:delete",
    "operating.cycle-review:read",
    "operating.cycle-review:write",
    "operating.cycle-review:delete",
    "operating.kpi:read",
    "operating.kpi:write",
    "operating.kpi:delete",
    "operating.kpi-measurement:read",
    "operating.kpi-measurement:write",
    "operating.kpi-measurement:delete",
    "graph.operating:write",
)

INTAKE_OWNER = (
    "intake.source:read",
    "intake.source:write",
    "intake.source:delete",
    "intake.intake:read",
    "intake.intake:write",
    "intake.intake:delete",
    "intake.intake-event:read",
    "intake.intake-event:write",
    "intake.intake-event:delete",
)

AUTOMATION_OWNER = (
    "automation.cron-job:read",
    "automation.cron-job:write",
    "automation.cron-job:delete",
    "automation.automation-rule:read",
    "automation.automation-rule:write",
    "automation.automation-rule:delete",
    "automation.workflow-pipeline:read",
    "automation.workflow-pipeline:write",
    "automation.workflow-pipeline:delete",
    "automation.automation-log:read",
    "automation.automation-log:write",
    "automation.automation-log:delete",
)

CONTENT_OWNER = (
    "content.content:read",
    "content.content:write",
    "content.content:delete",
    "content.content-calendar:read",
    "content.content-calendar:write",
    "content.content-calendar:delete",
    "content.content-series-episode:read",
    "content.content-series-episode:write",
    "content.content-series-episode:delete",
    "content.content-series-episode-metric:read",
    "content.content-series-episode-metric:write",
    "content.content-series-episode-metric:delete",
)

LEARNING_OWNER = (
    "learning.challenge:read",
    "learning.challenge:write",
    "learning.challenge:delete",
    "learning.challenge-assignment:read",
    "learning.challenge-assignment:write",
    "learning.challenge-assignment:delete",
)

DIARY_OWNER = (
    "diary.journal-entry:read",
    "diary.journal-entry:write",
    "diary.journal-entry:delete",
)

OPPORTUNITIES_OWNER = (
    "opportunities.opportunity:read",
    "opportunities.opportunity:write",
    "opportunities.opportunity:delete",
    "opportunities.opportunity-action:read",
    "opportunities.opportunity-action:write",
    "opportunities.opportunity-action:delete",
)

HEALTH_OWNER = (
    "health.health-metric:read",
    "health.health-metric:write",
    "health.health-metric:delete",
)

TIME_OWNER = (
    "time.time-entry:read",
    "time.time-entry:write",
    "time.time-entry:delete",
    "time.time-log:read",
    "time.time-log:write",
    "time.time-log:delete",
)

RADAR_OWNER = (
    "radar.opportunity:read",
    "radar.opportunity:write",
    "radar.opportunity:delete",
    "radar.profile:read",
    "radar.profile:write",
    "radar.profile:delete",
    "radar.opportunity-status:read",
    "radar.opportunity-status:write",
    "radar.opportunity-status:delete",
)

# Owner permissions per cell; `empire-cells grant` adds them to a workspace's owner role.
CELL_OWNERS: dict[str, tuple[str, ...]] = {}

TASKS_OWNER = (
    "tasks.task:read",
    "tasks.task:write",
    "tasks.task:delete",
    "tasks.status:write",
    "tasks.calendar:write",
    "tasks.link:write",
    "tasks.archive:write",
    "tasks.reminder:write",
    "tasks.occurrence:write",
    "tasks.assignment:write",
    "tasks.suggestion:write",
)

CELL_OWNERS.update(
    tasks=TASKS_OWNER,
    decisions=DECISIONS_OWNER,
    habits=HABITS_OWNER,
    notifications=NOTIFICATIONS_OWNER,
    goals=GOALS_OWNER,
    projects=PROJECTS_OWNER,
)
CELL_OWNERS["roles-skills"] = ROLES_SKILLS_OWNER
CELL_OWNERS.update(
    operating=OPERATING_OWNER, intake=INTAKE_OWNER, automation=AUTOMATION_OWNER
)
CELL_OWNERS["accounts"] = ACCOUNTS_OWNER
CELL_OWNERS.update(
    content=CONTENT_OWNER,
    learning=LEARNING_OWNER,
    diary=DIARY_OWNER,
    opportunities=OPPORTUNITIES_OWNER,
    health=HEALTH_OWNER,
    time=TIME_OWNER,
)

# Finance, split into ten cells on 2026-10-12 (one repository, API, Neon project and MCP each).
FINANCE_OWNERS: dict[str, tuple[str, ...]] = {
    "finance-accounts": (
        "finance-accounts.account:read",
        "finance-accounts.account:write",
        "finance-accounts.account:delete",
    ),
    "transactions": (
        "transactions.transaction:read",
        "transactions.transaction:write",
        "transactions.transaction:delete",
    ),
    "budgets": (
        "budgets.budget-plan:read",
        "budgets.budget-plan:write",
        "budgets.budget-plan:delete",
    ),
    "cash-flow": (
        "cash-flow.cashflow-plan:read",
        "cash-flow.cashflow-plan:write",
        "cash-flow.cashflow-plan:delete",
    ),
    "reconciliations": (
        "reconciliations.reconciliation:read",
        "reconciliations.reconciliation:write",
        "reconciliations.reconciliation:delete",
    ),
    "business-assets": (
        "business-assets.asset:read",
        "business-assets.asset:write",
        "business-assets.asset:delete",
        "business-assets.asset-metric:read",
        "business-assets.asset-metric:write",
        "business-assets.asset-metric:delete",
    ),
    "investments": (
        "investments.investment:read",
        "investments.investment:write",
        "investments.investment:delete",
        "investments.market-price:read",
        "investments.market-price:write",
        "investments.market-price:delete",
        "investments.valuation:read",
        "investments.valuation:write",
        "investments.valuation:delete",
    ),
    "liabilities": (
        "liabilities.liability:read",
        "liabilities.liability:write",
        "liabilities.liability:delete",
    ),
    "subscriptions": (
        "subscriptions.subscription:read",
        "subscriptions.subscription:write",
        "subscriptions.subscription:delete",
        "subscriptions.subscription-review:read",
        "subscriptions.subscription-review:write",
        "subscriptions.subscription-review:delete",
        "subscriptions.subscription-alias:read",
        "subscriptions.subscription-alias:write",
        "subscriptions.subscription-alias:delete",
    ),
    "tax": (
        "tax.tax-item:read",
        "tax.tax-item:write",
        "tax.tax-item:delete",
    ),
}
CELL_OWNERS.update(FINANCE_OWNERS)
CELL_OWNERS["radar"] = RADAR_OWNER
