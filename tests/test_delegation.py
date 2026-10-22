"""Agents act only through delegations that never exceed the grantor."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from conftest import (
    add_member,
    create_workspace,
    delegate,
    has_permission,
    new_id,
    use_workspace,
)

READ, WRITE, DELETE = "tasks.task:read", "tasks.task:write", "tasks.task:delete"


@pytest.fixture
def workspace(api):
    ws = create_workspace(api, "marathon", [READ, WRITE])
    add_member(api, ws, "coach", "agent")
    add_member(api, ws, "partner", "human")
    return ws


def test_delegation_cannot_exceed_the_grantor(api, workspace):
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match=r"tasks\.task:delete"
    ):
        delegate(
            api, workspace, workspace.owner, workspace.members["coach"], [READ, DELETE]
        )


def test_grantor_without_any_matching_permission_cannot_delegate(api, workspace):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        delegate(
            api,
            workspace,
            workspace.members["partner"],
            workspace.members["coach"],
            [READ],
        )


def test_delegation_only_goes_to_non_human_members(api, workspace):
    with pytest.raises(psycopg.errors.CheckViolation):
        delegate(api, workspace, workspace.owner, workspace.members["partner"], [READ])


def test_delegation_requires_the_grantee_to_be_a_member(api, workspace):
    outsider = new_id(api)
    use_workspace(api, workspace.id)
    api.execute(
        "INSERT INTO platform.identities (id, kind, display_name) VALUES (%s, 'agent', 'stranger')",
        (outsider,),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        delegate(api, workspace, workspace.owner, outsider, [READ])


def test_partial_delegation_grants_only_its_scopes(api, workspace):
    coach = workspace.members["coach"]
    delegation = delegate(api, workspace, workspace.owner, coach, [READ])
    assert has_permission(api, coach, READ, delegation)
    assert not has_permission(api, coach, WRITE, delegation)
    assert not has_permission(api, coach, READ)  # no delegation, no permission


def test_agent_roles_never_grant_direct_permissions(api, workspace):
    coach = workspace.members["coach"]
    use_workspace(api, workspace.id)
    api.execute(
        "INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (%s, %s, %s)",
        (workspace.id, coach, workspace.owner_role),
    )
    assert not has_permission(api, coach, READ)


def test_delegation_follows_the_grantor_losing_a_permission(api, workspace):
    coach = workspace.members["coach"]
    delegation = delegate(api, workspace, workspace.owner, coach, [READ, WRITE])
    api.execute(
        "DELETE FROM platform.role_permissions WHERE role_id = %s AND permission = %s",
        (workspace.owner_role, WRITE),
    )
    assert has_permission(api, coach, READ, delegation)
    assert not has_permission(api, coach, WRITE, delegation)


def test_expired_or_revoked_delegations_grant_nothing(api, workspace):
    coach = workspace.members["coach"]
    expired = delegate(
        api, workspace, workspace.owner, coach, [READ], expires_in=timedelta(seconds=-1)
    )
    assert not has_permission(api, coach, READ, expired)
    revoked = delegate(api, workspace, workspace.owner, coach, [READ])
    api.execute(
        "UPDATE platform.delegations SET revoked_at = now() WHERE id = %s", (revoked,)
    )
    assert not has_permission(api, coach, READ, revoked)


def test_suspended_agent_loses_delegated_permissions(api, workspace):
    coach = workspace.members["coach"]
    delegation = delegate(api, workspace, workspace.owner, coach, [READ])
    api.execute(
        "UPDATE platform.memberships SET status = 'suspended' WHERE workspace_id = %s AND identity_id = %s",
        (workspace.id, coach),
    )
    assert not has_permission(api, coach, READ, delegation)


def test_delegation_is_useless_in_another_workspace(api, workspace):
    coach = workspace.members["coach"]
    delegation = delegate(api, workspace, workspace.owner, coach, [READ])
    other = create_workspace(api, "other", [READ])
    use_workspace(api, other.id)
    assert not has_permission(api, coach, READ, delegation)


def test_widening_an_existing_delegation_is_checked_again(api, workspace):
    coach = workspace.members["coach"]
    delegation = delegate(api, workspace, workspace.owner, coach, [READ])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute(
            "UPDATE platform.delegations SET scopes = %s::platform.permission[] WHERE id = %s",
            ([READ, DELETE], delegation),
        )


def insert_execution(api, workspace, executed_by, on_behalf_of, delegation=None):
    api.execute(
        "INSERT INTO platform.executions (workspace_id, initiated_by, executed_by, on_behalf_of, delegation_id, channel)"
        " VALUES (%s, %s, %s, %s, %s, 'workflow')",
        (workspace.id, workspace.owner, executed_by, on_behalf_of, delegation),
    )


def test_agent_cannot_act_for_someone_without_their_delegation(api, workspace):
    coach = workspace.members["coach"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), api.transaction():
        insert_execution(api, workspace, coach, workspace.owner)
    delegation = delegate(api, workspace, workspace.owner, coach, [READ])
    insert_execution(api, workspace, coach, workspace.owner, delegation)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        insert_execution(
            api, workspace, coach, workspace.members["partner"], delegation
        )
