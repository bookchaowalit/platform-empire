"""Book Platform services: shared scopes map to canonical permissions the owner role holds."""

import json
import re

from empire_platform.permissions import CELL_OWNERS, PLATFORM_SHARED_SCOPES

# platform-identity's canonical permission pattern (app.py PERMISSION).
CANONICAL = re.compile(
    r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)$"
)
SERVICES = (
    "communication",
    "media",
    "data",
    "workflow",
    "commerce",
    "logistics",
    "developer",
)


def test_every_shared_scope_maps_to_a_canonical_permission():
    assert PLATFORM_SHARED_SCOPES
    for scope, permission in PLATFORM_SHARED_SCOPES.items():
        assert not CANONICAL.match(scope), scope
        assert CANONICAL.match(permission), permission


def test_owner_role_holds_every_mapped_permission():
    owned = {p for service in SERVICES for p in CELL_OWNERS[service]}
    assert set(PLATFORM_SHARED_SCOPES.values()) <= owned


def test_scopes_the_services_check_are_all_mapped():
    expected = {
        "messages:read",
        "messages:write",
        "opt-outs:write",
        "files:read",
        "files:write",
        "cache:read",
        "cache:write",
        "counters:write",
        "documents:read",
        "documents:write",
        "quotas:write",
        "quotas:reserve",
        "quotas:settle",
        "jobs:read",
        "jobs:write",
        "jobs:work",
        "catalog:read",
        "catalog:write",
        "cart:read",
        "cart:write",
        "orders:read",
        "orders:write",
        "promotions:read",
        "promotions:write",
        "rates:read",
        "shipments:read",
        "shipments:write",
        "labels:write",
        "tracking:write",
        "flags:read",
        "flags:write",
    }
    assert set(PLATFORM_SHARED_SCOPES) == expected


def test_identity_limits_hold():
    # IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON: at most 64 mappings, 16 KiB, scopes <= 128 chars.
    assert len(PLATFORM_SHARED_SCOPES) <= 64
    assert len(json.dumps(PLATFORM_SHARED_SCOPES)) <= 16384
    assert all(len(s) <= 128 for s in PLATFORM_SHARED_SCOPES)
