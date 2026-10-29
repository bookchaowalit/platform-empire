"""Platform service audiences (deployed 2026-10-29) must be in the default token audiences."""

from empire_platform.devtoken import DEFAULT_AUDIENCES

PLATFORM_AUDIENCES = (
    "communication-api",
    "media-api",
    "data-api",
    "workflow-api",
    "commerce-api",
    "logistics-api",
    "developer-api",
)


def test_platform_service_audiences_are_default():
    missing = [aud for aud in PLATFORM_AUDIENCES if aud not in DEFAULT_AUDIENCES]
    assert missing == []


def test_default_audiences_have_no_duplicates():
    assert len(DEFAULT_AUDIENCES) == len(set(DEFAULT_AUDIENCES))
