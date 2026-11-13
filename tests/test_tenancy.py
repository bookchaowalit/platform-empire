"""SPEC-006 (platform-identity): tenancy functions in migration 0009.

identity_api calls these SECURITY DEFINER functions; it still has no table write
grant. Every caller identity and workspace argument comes from a verified token
in identity, so these tests call the functions the same way identity does.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

OWNER_PERMS = [
    "identity.member:read",
    "identity.member:admin",
    "identity.workspace:admin",
    "identity.installation:read",
    "decisions.decision:read",
    "decisions.decision:write",
]
ROLES = {
    "owner": OWNER_PERMS,
    "admin": [p for p in OWNER_PERMS if p != "identity.workspace:admin"],
    "member": ["decisions.decision:read", "decisions.decision:write"],
    "billing": ["payments.intent:write"],  # a role holding more than the admin has
}


def install(client_id: str, perms: list[str] | None = None) -> dict[str, Any]:
    return {
        "subject": str(uuid.uuid4()),
        "client_id": client_id,
        "display_name": client_id,
        "permissions": perms or ["decisions.decision:read"],
        "products": ["finance"],
    }


class Code(Exception):
    pass


def call(cluster, fn: str, *args: Any) -> Any:
    """Call platform.<fn> as identity_api in one committed transaction; the error text is the code."""
    placeholders = ", ".join(["%s"] * len(args))
    with cluster.connect("identity_api") as conn:
        try:
            row = conn.execute(
                f"SELECT platform.{fn}({placeholders})",  # nosemgrep: fixed function names in tests
                [Jsonb(a) if isinstance(a, dict) else a for a in args],
            ).fetchone()
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            raise Code(exc.diag.message_primary) from None
    return row[0] if row else None


def su(cluster, query: str, params: tuple = ()) -> list[tuple]:
    with cluster.connect("postgres", "empire") as conn:
        cur = conn.execute(query, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows


def signup(
    cluster,
    subject: str | None = None,
    product: str = "finance",
    slug: str | None = None,
    installs: list[dict[str, Any]] | None = None,
    max_owned: int = 3,
    subject_hmacs: list[str] | None = None,
) -> dict[str, Any]:
    subject = subject or f"google-{uuid.uuid4().hex}"
    slug = slug or f"ws-{uuid.uuid4().hex[:10]}"
    return call(
        cluster,
        "tenancy_signup",
        subject,
        subject_hmacs or [f"h:{subject}"],
        "Ann",
        product,
        slug,
        "Ann's books",
        max_owned,
        ROLES,
        Jsonb(installs or []),
    )


def invite(
    cluster,
    caller,
    ws,
    email="ann@example.com",
    role="member",
    ttl=7 * 86400,
    code=None,
):
    code = code or f"code-{uuid.uuid4().hex}"
    out = call(
        cluster,
        "tenancy_invite",
        caller,
        ws,
        f"e:{email}",
        "an…@e…",
        f"c:{code}",
        role,
        ttl,
    )
    return out, code


def accept(cluster, subject, code, email="ann@example.com"):
    return call(
        cluster, "tenancy_accept", subject, "Bob", [f"c:{code}"], [f"e:{email}"]
    )


def roles_of(cluster, ws, identity) -> set[str]:
    rows = su(
        cluster,
        "SELECT r.name FROM platform.membership_roles mr JOIN platform.roles r ON r.id = mr.role_id"
        " WHERE mr.workspace_id = %s AND mr.identity_id = %s",
        (ws, identity),
    )
    return {r[0] for r in rows}


# --- role and privileges --------------------------------------------------------------------------


def test_platform_tenancy_role_is_nologin_without_bypass(cluster):
    row = su(
        cluster,
        "SELECT rolcanlogin, rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'platform_tenancy'",
    )
    assert row == [(False, False, False)]


def test_identity_api_has_no_table_writes(cluster):
    rows = su(
        cluster,
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
        " WHERE grantee = 'identity_api' AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE')",
    )
    assert rows == []


def test_tenancy_functions_are_not_public(cluster):
    rows = su(
        cluster,
        "SELECT p.proname, has_function_privilege('platform_api', p.oid, 'EXECUTE'),"
        " has_function_privilege('identity_api', p.oid, 'EXECUTE'), p.prosecdef,"
        " pg_get_userbyid(p.proowner)"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'platform' AND p.proname LIKE 'tenancy\\_%%'",
    )
    entry = {
        "tenancy_signup",
        "tenancy_my_workspaces",
        "tenancy_invite",
        "tenancy_list_invitations",
        "tenancy_revoke_invitation",
        "tenancy_accept",
        "tenancy_list_members",
        "tenancy_set_member",
        "tenancy_remove_member",
        "tenancy_update_workspace",
        "tenancy_installations",
        "tenancy_active",
        "tenancy_install",
    }
    assert entry <= {r[0] for r in rows}
    for name, api, identity, secdef, owner in rows:
        # identity_api reaches the entry points only; helpers run as their owner inside them.
        assert (api, identity, secdef, owner) == (
            False,
            name in entry,
            True,
            "platform_tenancy",
        ), name
    acl = su(
        cluster,
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'platform' AND p.proname LIKE 'tenancy\\_%%'"
        " AND has_function_privilege('public', p.oid, 'EXECUTE')",
    )
    assert acl == [(0,)]


# --- signup (AC-01..AC-05) ------------------------------------------------------------------------


def test_signup_creates_workspace(cluster):
    out = signup(cluster)
    assert out["created"] is True
    assert set(out) == {"workspace_id", "slug", "identity_id", "created"}


def test_signup_rows_and_atomicity(cluster):
    worker = install("communication-worker")
    out = signup(cluster, installs=[worker])
    ws, me = out["workspace_id"], out["identity_id"]
    assert su(
        cluster, "SELECT kind, status FROM platform.identities WHERE id = %s", (me,)
    ) == [("human", "active")]
    assert su(
        cluster,
        "SELECT owner_identity_id::text, status, product FROM platform.workspaces WHERE id = %s",
        (ws,),
    ) == [(me, "active", "finance")]
    assert {
        r[0]
        for r in su(
            cluster, "SELECT name FROM platform.roles WHERE workspace_id = %s", (ws,)
        )
    } == {"owner", "admin", "member", "billing", "installation:communication-worker"}
    assert roles_of(cluster, ws, me) == {"owner"}
    assert roles_of(cluster, ws, worker["subject"]) == {
        "installation:communication-worker"
    }
    assert su(
        cluster,
        "SELECT kind FROM platform.identities WHERE id = %s",
        (worker["subject"],),
    ) == [("service_account",)]
    assert su(
        cluster,
        "SELECT action FROM platform.audit_events WHERE workspace_id = %s",
        (ws,),
    ) == [("workspace.created",)]
    # A bad installation permission aborts the whole signup: no identity, workspace or signup row.
    subject = f"google-{uuid.uuid4().hex}"
    with pytest.raises(Code):
        signup(
            cluster, subject=subject, installs=[install("bad", ["not a permission"])]
        )
    assert su(
        cluster,
        "SELECT count(*) FROM platform.identities WHERE external_subject = %s",
        (subject,),
    ) == [(0,)]


def test_signup_replay_returns_existing(cluster):
    subject = f"google-{uuid.uuid4().hex}"
    first = signup(cluster, subject=subject)
    again = signup(cluster, subject=subject, slug="some-other-slug")
    assert again == {**first, "created": False}
    other_product = signup(cluster, subject=subject, product="crm")
    assert other_product["created"] is True
    assert other_product["workspace_id"] != first["workspace_id"]


def test_signup_conflicts_and_limits(cluster):
    taken = signup(cluster)
    with pytest.raises(Code, match="slug_taken"):
        signup(cluster, slug=taken["slug"])
    subject = f"google-{uuid.uuid4().hex}"
    for product in ("p1", "p2"):
        signup(cluster, subject=subject, product=product, max_owned=2)
    with pytest.raises(Code, match="workspace_limit"):
        signup(cluster, subject=subject, product="p3", max_owned=2)


def test_concurrent_signups_same_slug_one_wins(cluster):
    slug, results = f"race-{uuid.uuid4().hex[:8]}", []

    def run():
        try:
            results.append(signup(cluster, slug=slug)["created"])
        except Code as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(map(str, results)) == ["True", "slug_taken"]


def test_disabled_identity_cannot_sign_up(cluster):
    subject = f"google-{uuid.uuid4().hex}"
    me = signup(cluster, subject=subject)["identity_id"]
    su(
        cluster,
        "UPDATE platform.identities SET status = 'disabled' WHERE id = %s",
        (me,),
    )
    with pytest.raises(Code, match="invalid_grant"):
        signup(cluster, subject=subject, product="other")


# --- my workspaces (AC-06) -------------------------------------------------------------------------


def test_list_my_workspaces(cluster):
    subject = f"google-{uuid.uuid4().hex}"
    a = signup(cluster, subject=subject, product="pa")
    b = signup(cluster, subject=subject, product="pb")
    d = signup(cluster, subject=subject, product="pd")
    # c: invited to someone else's workspace, then suspended there
    c = signup(cluster, product="pc")
    _, code = invite(
        cluster, c["identity_id"], c["workspace_id"], email="me@example.com"
    )
    accept(cluster, subject, code, email="me@example.com")
    call(
        cluster,
        "tenancy_set_member",
        c["identity_id"],
        c["workspace_id"],
        a["identity_id"],
        None,
        "suspended",
    )
    call(
        cluster,
        "tenancy_update_workspace",
        d["identity_id"],
        d["workspace_id"],
        None,
        "closed",
    )
    listed = call(cluster, "tenancy_my_workspaces", subject)
    assert {w["workspace_id"] for w in listed} == {a["workspace_id"], b["workspace_id"]}
    assert all(
        set(w) == {"workspace_id", "slug", "name", "product", "status", "roles"}
        for w in listed
    )
    assert call(cluster, "tenancy_my_workspaces", "google-unknown") == []


# --- invitations (AC-07..AC-09) ---------------------------------------------------------------------


def test_invitation_create_and_list(cluster):
    t = signup(cluster)
    out, _ = invite(cluster, t["identity_id"], t["workspace_id"])
    assert set(out) == {"invitation_id", "expires_at"}
    listed = call(
        cluster, "tenancy_list_invitations", t["identity_id"], t["workspace_id"]
    )
    assert [set(i) for i in listed] == [
        {"invitation_id", "role", "email_hint", "status", "expires_at"}
    ]
    stored = su(
        cluster,
        "SELECT email_hmac, code_hmac FROM platform.invitations WHERE id = %s",
        (out["invitation_id"],),
    )[0]
    assert stored[0].startswith("e:")
    assert stored[1].startswith("c:")
    for role in ("owner", "installation:x", "nope"):
        with pytest.raises(Code, match="invalid_role"):
            invite(cluster, t["identity_id"], t["workspace_id"], role=role)


def test_member_without_admin_cannot_invite(cluster):
    t = signup(cluster)
    _, code = invite(
        cluster, t["identity_id"], t["workspace_id"], email="m@example.com"
    )
    member = accept(
        cluster, "google-member-" + uuid.uuid4().hex, code, email="m@example.com"
    )
    member_id = su(
        cluster,
        "SELECT identity_id FROM platform.memberships WHERE workspace_id = %s"
        " AND identity_id <> %s",
        (member["workspace_id"], t["identity_id"]),
    )[0][0]
    with pytest.raises(Code, match="forbidden"):
        invite(cluster, member_id, t["workspace_id"])


def test_accept_invitation_once(cluster):
    t = signup(cluster)
    _, code = invite(cluster, t["identity_id"], t["workspace_id"], role="admin")
    subject = f"google-{uuid.uuid4().hex}"
    assert accept(cluster, subject, code) == {"workspace_id": t["workspace_id"]}
    me = su(
        cluster,
        "SELECT id FROM platform.identities WHERE external_subject = %s",
        (subject,),
    )[0][0]
    assert roles_of(cluster, t["workspace_id"], me) == {"admin"}
    with pytest.raises(Code, match="invalid_grant"):
        accept(cluster, f"google-{uuid.uuid4().hex}", code)  # used
    _, wrong_email = invite(cluster, t["identity_id"], t["workspace_id"])
    with pytest.raises(Code, match="invalid_grant"):
        accept(
            cluster, f"google-{uuid.uuid4().hex}", wrong_email, email="eve@example.com"
        )
    with pytest.raises(Code, match="invalid_grant"):
        accept(cluster, f"google-{uuid.uuid4().hex}", "unknown-code")


def test_concurrent_accepts_give_one_success(cluster):
    t = signup(cluster)
    _, code = invite(cluster, t["identity_id"], t["workspace_id"])
    results = []

    def run():
        try:
            results.append(
                "ok" if accept(cluster, f"google-{uuid.uuid4().hex}", code) else "?"
            )
        except Code as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["invalid_grant", "ok"]


def test_expired_or_inactive_workspace_invitation_refused(cluster):
    t = signup(cluster)
    _, code = invite(cluster, t["identity_id"], t["workspace_id"], ttl=1)
    su(
        cluster,
        "UPDATE platform.invitations SET expires_at = now() - interval '1 second' WHERE code_hmac = %s",
        (f"c:{code}",),
    )
    with pytest.raises(Code, match="invalid_grant"):
        accept(cluster, f"google-{uuid.uuid4().hex}", code)
    _, code2 = invite(cluster, t["identity_id"], t["workspace_id"])
    call(
        cluster,
        "tenancy_update_workspace",
        t["identity_id"],
        t["workspace_id"],
        None,
        "suspended",
    )
    with pytest.raises(Code, match="invalid_grant"):
        accept(cluster, f"google-{uuid.uuid4().hex}", code2)


def test_invitation_to_existing_member_is_noop(cluster):
    subject = f"google-{uuid.uuid4().hex}"
    t = signup(cluster, subject=subject)
    _, code = invite(cluster, t["identity_id"], t["workspace_id"])
    assert accept(cluster, subject, code) == {"workspace_id": t["workspace_id"]}
    assert roles_of(cluster, t["workspace_id"], t["identity_id"]) == {"owner"}


def test_revoke_invitation(cluster):
    t, other = signup(cluster), signup(cluster)
    out, code = invite(cluster, t["identity_id"], t["workspace_id"])
    with pytest.raises(Code, match="not_found"):
        call(
            cluster,
            "tenancy_revoke_invitation",
            other["identity_id"],
            other["workspace_id"],
            out["invitation_id"],
        )
    call(
        cluster,
        "tenancy_revoke_invitation",
        t["identity_id"],
        t["workspace_id"],
        out["invitation_id"],
    )
    with pytest.raises(Code, match="invalid_grant"):
        accept(cluster, f"google-{uuid.uuid4().hex}", code)


# --- members (AC-10, AC-11) --------------------------------------------------------------------------


def _with_member(cluster, role="member", installs=None):
    t = signup(cluster, installs=installs)
    email = f"{uuid.uuid4().hex[:6]}@example.com"
    _, code = invite(
        cluster, t["identity_id"], t["workspace_id"], email=email, role=role
    )
    subject = f"google-{uuid.uuid4().hex}"
    accept(cluster, subject, code, email=email)
    member = su(
        cluster,
        "SELECT id FROM platform.identities WHERE external_subject = %s",
        (subject,),
    )[0][0]
    return t, str(member)


def test_members_list_patch_delete(cluster):
    worker = install("worker")
    t, member = _with_member(cluster, installs=[worker])
    ws, owner = t["workspace_id"], t["identity_id"]
    listed = call(cluster, "tenancy_list_members", owner, ws)
    assert {m["identity_id"] for m in listed} == {owner, member, worker["subject"]}
    assert all(
        set(m) == {"identity_id", "kind", "display_name", "status", "roles"}
        for m in listed
    )
    call(cluster, "tenancy_set_member", owner, ws, member, ["admin"], None)
    assert roles_of(cluster, ws, member) == {"admin"}
    call(cluster, "tenancy_set_member", owner, ws, member, None, "suspended")
    assert call(cluster, "tenancy_active", member, ws, False) is None
    call(cluster, "tenancy_set_member", owner, ws, member, None, "active")
    assert call(cluster, "tenancy_active", member, ws, False) == "human"
    with pytest.raises(Code, match="installation_fixed"):
        call(
            cluster, "tenancy_set_member", owner, ws, worker["subject"], ["admin"], None
        )
    with pytest.raises(Code, match="invalid_role"):
        call(
            cluster,
            "tenancy_set_member",
            owner,
            ws,
            member,
            ["installation:worker"],
            None,
        )
    call(cluster, "tenancy_remove_member", owner, ws, worker["subject"])  # uninstall
    assert call(cluster, "tenancy_installations", worker["subject"], None, 500) == []
    call(cluster, "tenancy_remove_member", owner, ws, member)
    assert call(cluster, "tenancy_active", member, ws, False) is None
    assert (
        len(
            su(
                cluster,
                "SELECT 1 FROM platform.audit_events WHERE workspace_id = %s",
                (ws,),
            )
        )
        >= 6
    )


def test_owner_rules(cluster):
    t, admin = _with_member(cluster, role="admin")
    ws, owner = t["workspace_id"], t["identity_id"]
    with pytest.raises(Code, match="forbidden"):
        call(
            cluster, "tenancy_set_member", admin, ws, admin, ["owner"], None
        )  # only owners grant owner
    for change in ((["admin"], None), (None, "suspended")):
        with pytest.raises(Code, match="last_owner"):
            call(cluster, "tenancy_set_member", owner, ws, owner, *change)
    with pytest.raises(Code, match="last_owner"):
        call(cluster, "tenancy_remove_member", owner, ws, owner)
    with pytest.raises(Code, match="forbidden"):
        call(cluster, "tenancy_remove_member", admin, ws, owner)


def test_concurrent_demotion_leaves_an_owner(cluster):
    t, second = _with_member(cluster, role="admin")
    ws, first = t["workspace_id"], t["identity_id"]
    call(cluster, "tenancy_set_member", first, ws, second, ["owner"], None)
    results = []

    def demote(caller, target):
        try:
            call(cluster, "tenancy_set_member", caller, ws, target, ["admin"], None)
            results.append("ok")
        except Code as exc:
            results.append(str(exc))

    threads = [
        threading.Thread(target=demote, args=(first, second)),
        threading.Thread(target=demote, args=(second, first)),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    owners = su(
        cluster,
        "SELECT count(*) FROM platform.membership_roles mr JOIN platform.roles r ON r.id = mr.role_id"
        " WHERE mr.workspace_id = %s AND r.name = 'owner'",
        (ws,),
    )[0][0]
    assert owners >= 1
    assert "ok" in results


# --- workspace status (AC-12, AC-13) ------------------------------------------------------------------


def test_workspace_patch_status(cluster):
    t, admin = _with_member(cluster, role="admin")
    ws, owner = t["workspace_id"], t["identity_id"]
    with pytest.raises(Code, match="forbidden"):
        call(cluster, "tenancy_update_workspace", admin, ws, "new", None)
    call(cluster, "tenancy_update_workspace", owner, ws, "Renamed", None)
    call(cluster, "tenancy_update_workspace", owner, ws, None, "suspended")
    assert call(cluster, "tenancy_active", owner, ws, False) is None
    assert (
        call(cluster, "tenancy_active", owner, ws, True) == "human"
    )  # owner may reactivate
    assert call(cluster, "tenancy_active", admin, ws, True) is None
    with pytest.raises(Code, match="forbidden"):
        call(cluster, "tenancy_update_workspace", admin, ws, None, "active")
    call(cluster, "tenancy_update_workspace", owner, ws, None, "active")
    assert call(cluster, "tenancy_active", owner, ws, False) == "human"
    call(cluster, "tenancy_update_workspace", owner, ws, None, "closed")
    with pytest.raises(Code, match="workspace_closed"):
        call(cluster, "tenancy_update_workspace", owner, ws, None, "active")
    assert call(cluster, "tenancy_active", owner, ws, True) is None


def test_disabled_identity_is_not_active(cluster):
    t = signup(cluster)
    su(
        cluster,
        "UPDATE platform.identities SET status = 'disabled' WHERE id = %s",
        (t["identity_id"],),
    )
    assert (
        call(cluster, "tenancy_active", t["identity_id"], t["workspace_id"], False)
        is None
    )


# --- isolation (AC-14) --------------------------------------------------------------------------------


def test_isolation_and_fail_closed(cluster):
    one, two = signup(cluster), signup(cluster)
    out, _ = invite(cluster, two["identity_id"], two["workspace_id"])
    # An owner of W1 naming W2 is not a member of W2: forbidden/not found, nothing changes.
    with pytest.raises(Code, match="forbidden"):
        call(cluster, "tenancy_list_members", one["identity_id"], two["workspace_id"])
    with pytest.raises(Code, match="not_found"):
        call(
            cluster,
            "tenancy_set_member",
            one["identity_id"],
            one["workspace_id"],
            two["identity_id"],
            None,
            "suspended",
        )
    with pytest.raises(Code, match="not_found"):
        call(
            cluster,
            "tenancy_revoke_invitation",
            one["identity_id"],
            one["workspace_id"],
            out["invitation_id"],
        )
    assert (
        call(cluster, "tenancy_active", two["identity_id"], two["workspace_id"], False)
        == "human"
    )
    # identity_api still sees only one workspace at a time through RLS, and none when unset.
    with cluster.connect("identity_api") as conn:
        assert (
            conn.execute("SELECT count(*) FROM platform.memberships").fetchone()[0] == 0
        )


# --- installations (AC-20, AC-21, AC-23) -------------------------------------------------------------


def test_installations_paged_and_own(cluster):
    worker = install("pager")
    ids = [signup(cluster, installs=[dict(worker)])["workspace_id"] for _ in range(3)]
    other = install("someone-else")
    signup(cluster, installs=[other])
    page1 = call(cluster, "tenancy_installations", worker["subject"], None, 2)
    page2 = call(cluster, "tenancy_installations", worker["subject"], page1[-1], 2)
    assert page1 + page2 == sorted(ids)
    assert len(page1) == 2
    assert (
        call(cluster, "tenancy_active", worker["subject"], ids[0], False)
        == "service_account"
    )


def test_install_and_reinstall(cluster):
    t, member = _with_member(cluster, role="member")
    ws, owner = t["workspace_id"], t["identity_id"]
    client = install("late-client")
    assert call(cluster, "tenancy_install", owner, ws, client) == {"created": True}
    assert call(cluster, "tenancy_install", owner, ws, client) == {"created": False}
    assert call(cluster, "tenancy_installations", client["subject"], None, 10) == [ws]
    with pytest.raises(Code, match="forbidden"):
        call(cluster, "tenancy_install", member, ws, install("x"))


def test_install_only_for_the_workspace_product(cluster):
    t = signup(cluster)
    with pytest.raises(Code, match="invalid_client"):
        call(
            cluster,
            "tenancy_install",
            t["identity_id"],
            t["workspace_id"],
            {**install("crm-only"), "products": ["crm"]},
        )


def test_install_needs_template_permissions(cluster):
    t = signup(cluster)
    with pytest.raises(Code, match="insufficient_privilege"):
        call(
            cluster,
            "tenancy_install",
            t["identity_id"],
            t["workspace_id"],
            install("greedy", ["payments.intent:write"]),
        )


def test_existing_human_cannot_become_installation(cluster):
    t = signup(cluster)
    with pytest.raises(Code, match="invalid_client"):
        call(
            cluster,
            "tenancy_install",
            t["identity_id"],
            t["workspace_id"],
            {**install("takeover"), "subject": t["identity_id"]},
        )


def test_expires_at_is_in_future(cluster):
    t = signup(cluster)
    out, _ = invite(cluster, t["identity_id"], t["workspace_id"], ttl=3600)
    expires = datetime.fromisoformat(out["expires_at"])
    assert timedelta(minutes=59) < expires - datetime.now(UTC) <= timedelta(hours=1)


# --- review fixes (code review 2026-11-12) ----------------------------------------------------------


def test_admin_cannot_grant_a_role_beyond_its_own_permissions(cluster):
    t, admin = _with_member(cluster, role="admin")
    ws, owner = t["workspace_id"], t["identity_id"]
    with pytest.raises(Code, match="insufficient_privilege"):
        invite(cluster, admin, ws, role="billing")
    with pytest.raises(Code, match="insufficient_privilege"):
        call(
            cluster, "tenancy_set_member", admin, ws, admin, ["admin", "billing"], None
        )
    # The owner may (owners administer every role of the workspace).
    invite(cluster, owner, ws, role="billing")
    call(cluster, "tenancy_set_member", owner, ws, admin, ["admin", "billing"], None)


def test_suspended_owner_is_still_protected_from_admins(cluster):
    t, second = _with_member(cluster, role="admin")
    ws, first = t["workspace_id"], t["identity_id"]
    call(cluster, "tenancy_set_member", first, ws, second, ["owner"], None)
    call(cluster, "tenancy_set_member", first, ws, second, None, "suspended")
    _, code = invite(cluster, first, ws, email="adm@example.com", role="admin")
    accept(cluster, f"google-{uuid.uuid4().hex}", code, email="adm@example.com")
    admin = su(
        cluster,
        "SELECT accepted_by FROM platform.invitations WHERE code_hmac = %s",
        (f"c:{code}",),
    )[0][0]
    for change in ((["admin"], None), (None, "active")):
        with pytest.raises(Code, match=r"forbidden|insufficient_privilege"):
            call(cluster, "tenancy_set_member", str(admin), ws, second, *change)
    with pytest.raises(Code, match=r"forbidden|insufficient_privilege"):
        call(cluster, "tenancy_remove_member", str(admin), ws, second)


def test_signup_replay_survives_key_rotation(cluster):
    subject = f"google-{uuid.uuid4().hex}"
    first = signup(cluster, subject=subject, subject_hmacs=["old:" + subject])
    again = signup(
        cluster, subject=subject, subject_hmacs=["new:" + subject, "old:" + subject]
    )
    assert again == {**first, "created": False}


def test_signup_replay_survives_dropping_the_previous_key(cluster):
    # Review P2: after the rotation window the old HMAC is gone; the identity still finds its workspace.
    subject = f"google-{uuid.uuid4().hex}"
    first = signup(cluster, subject=subject, subject_hmacs=["old:" + subject])
    again = signup(
        cluster,
        subject=subject,
        slug=f"ws-{uuid.uuid4().hex[:10]}",
        subject_hmacs=["new:" + subject],
    )
    assert again == {**first, "created": False}


def test_reinstall_checks_the_existing_role(cluster):
    t, admin = _with_member(cluster, role="admin")
    ws, owner = t["workspace_id"], t["identity_id"]
    wide = install(
        "wide-client", ["identity.workspace:admin"]
    )  # more than an admin holds
    call(cluster, "tenancy_install", owner, ws, wide)
    call(cluster, "tenancy_remove_member", owner, ws, wide["subject"])
    with pytest.raises(Code, match="insufficient_privilege"):
        call(
            cluster,
            "tenancy_install",
            admin,
            ws,
            {**wide, "permissions": ["decisions.decision:read"]},
        )


def test_admin_cannot_manage_a_member_holding_more_than_it(cluster):
    t, admin = _with_member(cluster, role="admin")
    ws, owner = t["workspace_id"], t["identity_id"]
    _, code = invite(cluster, owner, ws, email="bill@example.com", role="billing")
    accept(cluster, f"google-{uuid.uuid4().hex}", code, email="bill@example.com")
    billing = str(
        su(
            cluster,
            "SELECT accepted_by FROM platform.invitations WHERE code_hmac = %s",
            (f"c:{code}",),
        )[0][0]
    )
    for change in ((None, "suspended"), (["member"], None)):
        with pytest.raises(Code, match="insufficient_privilege"):
            call(cluster, "tenancy_set_member", admin, ws, billing, *change)
    with pytest.raises(Code, match="insufficient_privilege"):
        call(cluster, "tenancy_remove_member", admin, ws, billing)
    call(
        cluster, "tenancy_set_member", owner, ws, billing, None, "suspended"
    )  # the owner may
