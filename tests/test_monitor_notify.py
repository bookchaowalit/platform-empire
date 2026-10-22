import json

import pytest

from empire_platform import monitor

REPORT = {
    "level": "breach",
    "checked_at": "2026-10-11T02:17:00+00:00",
    "findings": [
        {
            "level": "breach",
            "check": "token_expired",
            "workspace": None,
            "detail": "token x expired",
        }
    ],
}


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_breach_is_queued_once_per_day_without_leaking_the_token(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["auth"] = request.headers["Authorization"]
        return Response(b'{"outcome": "created"}')

    monkeypatch.setattr(monitor.urllib.request, "urlopen", fake_urlopen)
    assert monitor.notify(REPORT, "https://notes.example/", "tok-secret") == "created"
    assert seen["url"] == "https://notes.example/v1/notifications"
    assert seen["body"]["notification_type"] == "platform_check"
    assert seen["body"]["severity"] == "critical"
    assert seen["body"]["dedupe_key"] == "platform-check:2026-10-11:breach"
    assert "Runbook" in seen["body"]["message"]
    assert "tok-secret" not in json.dumps(seen["body"])


def test_api_errors_become_short_messages(monkeypatch):
    def broken(request, timeout):
        raise monitor.urllib.error.URLError("down")

    monkeypatch.setattr(monitor.urllib.request, "urlopen", broken)
    with pytest.raises(RuntimeError, match="unreachable") as raised:
        monitor.notify(REPORT, "https://notes.example", "tok")
    assert "tok" not in str(raised.value)
