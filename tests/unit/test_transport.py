from email.message import Message

import pytest

from calendar_sync.transport import (
    CalendarSyncSession,
    MutationBlockedError,
    Response,
    StdlibTransport,
    TargetContainmentGuard,
    TooManyRedirectsError,
)
from calendar_sync.urlsafety import canonicalize_url

TARGET = canonicalize_url("https://cloud.example.invalid/remote.php/dav/calendars/user/target/")
SOURCE = canonicalize_url("https://cloud.example.invalid/remote.php/dav/calendars/user/source/")
GUARD = TargetContainmentGuard(canonical_target=TARGET, canonical_sources=(SOURCE,))


def test_guard_allows_direct_child_of_target():
    GUARD.validate("https://cloud.example.invalid/remote.php/dav/calendars/user/target/event.ics")


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.example.invalid/remote.php/dav/calendars/user/source/event.ics",
        "https://cloud.example.invalid/remote.php/dav/calendars/user/target/sub/event.ics",
        "https://evil.invalid/remote.php/dav/calendars/user/target/event.ics",
        "https://cloud.example.invalid/remote.php/dav/calendars/user/target/../source/event.ics",
        "https://cloud.example.invalid/remote.php/dav/calendars/user/target/",
        "https://cloud.example.invalid:8443/remote.php/dav/calendars/user/target/event.ics",
    ],
)
def test_guard_blocks_unsafe_destinations(url):
    with pytest.raises(MutationBlockedError):
        GUARD.validate(url)


class _ScriptedTransport:
    """Minimal stand-in for StdlibTransport: returns queued responses (or
    raises queued exceptions) in order, and records every call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def send(self, method, url, *, data=None, headers=None, auth=None, guard=None):
        self.calls.append((method, url))
        if guard is not None and method in ("PUT", "POST", "PATCH", "DELETE"):
            guard.validate(url)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status, headers=None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return Response(status, "status", msg, b"")


def test_read_only_session_blocks_all_mutating_methods():
    session = CalendarSyncSession(_ScriptedTransport([]), allow_mutations=False)
    for method in ("PUT", "POST", "PATCH", "DELETE"):
        with pytest.raises(MutationBlockedError):
            session.request(method, "https://cloud.example.invalid/remote.php/dav/calendars/user/target/x.ics")


def test_mutating_session_requires_a_guard():
    with pytest.raises(ValueError):
        CalendarSyncSession(_ScriptedTransport([]), allow_mutations=True, guard=None)


def test_mutating_session_routes_through_guard():
    transport = _ScriptedTransport([_resp(201)])
    session = CalendarSyncSession(transport, allow_mutations=True, guard=GUARD)
    session.request("PUT", "https://cloud.example.invalid/remote.php/dav/calendars/user/target/x.ics", data=b"x")
    with pytest.raises(MutationBlockedError):
        session.request("PUT", "https://cloud.example.invalid/remote.php/dav/calendars/user/source/x.ics", data=b"x")


def test_get_retries_on_retryable_status_then_succeeds():
    transport = _ScriptedTransport([_resp(503), _resp(200)])
    session = CalendarSyncSession(transport, allow_mutations=False, base_delay=0.001, max_delay=0.001)
    response = session.request("GET", "https://cloud.example.invalid/remote.php/dav/calendars/user/source/")
    assert response.status_code == 200
    assert len(transport.calls) == 2


def test_get_retries_are_bounded():
    transport = _ScriptedTransport([_resp(503)] * 10)
    session = CalendarSyncSession(transport, allow_mutations=False, max_attempts=3, base_delay=0.001, max_delay=0.001)
    response = session.request("GET", "https://cloud.example.invalid/remote.php/dav/calendars/user/source/")
    assert response.status_code == 503
    assert len(transport.calls) == 3


def test_put_is_never_auto_retried_by_the_session():
    """PUT/DELETE preconditions make blind retry unsafe; only GET/PROPFIND/
    REPORT go through the retry wrapper (see CalendarSyncSession)."""
    transport = _ScriptedTransport([_resp(503)])
    session = CalendarSyncSession(transport, allow_mutations=True, guard=GUARD, max_attempts=5)
    response = session.request(
        "PUT", "https://cloud.example.invalid/remote.php/dav/calendars/user/target/x.ics", data=b"x"
    )
    assert response.status_code == 503
    assert len(transport.calls) == 1  # not retried


def _scripted_stdlib_transport(monkeypatch, responses_by_url):
    """Patch StdlibTransport._send_once to return queued (Response, url)
    pairs keyed by URL, so send()'s own redirect-following loop is what's
    under test - not the real socket layer."""
    transport = StdlibTransport(verify_tls=True, connect_timeout=1, read_timeout=1, user_agent="test/1.0")

    def fake_send_once(method, url, *, data, headers, auth):
        queue = responses_by_url[url]
        return queue.pop(0), url

    monkeypatch.setattr(transport, "_send_once", fake_send_once)
    return transport


def test_redirect_within_target_collection_is_followed(monkeypatch):
    start = "https://cloud.example.invalid/remote.php/dav/calendars/user/target/x.ics"
    final = "https://cloud.example.invalid/remote.php/dav/calendars/user/target/y.ics"
    redirect_headers = Message()
    redirect_headers["Location"] = final
    responses = {start: [_resp(302, {"Location": final})], final: [_resp(204)]}
    transport = _scripted_stdlib_transport(monkeypatch, responses)
    response = transport.send("PUT", start, data=b"x", guard=GUARD)
    assert response.status_code == 204


def test_redirect_outside_target_collection_is_blocked(monkeypatch):
    start = "https://cloud.example.invalid/remote.php/dav/calendars/user/target/x.ics"
    evil = "https://cloud.example.invalid/remote.php/dav/calendars/user/source/y.ics"
    responses = {start: [_resp(302, {"Location": evil})]}
    transport = _scripted_stdlib_transport(monkeypatch, responses)
    with pytest.raises(MutationBlockedError):
        transport.send("PUT", start, data=b"x", guard=GUARD)


def test_too_many_redirects_raises(monkeypatch):
    urls = [f"https://cloud.example.invalid/remote.php/dav/calendars/user/target/{i}.ics" for i in range(10)]
    responses = {
        urls[i]: [_resp(302, {"Location": urls[i + 1]})] for i in range(len(urls) - 1)
    }
    transport = _scripted_stdlib_transport(monkeypatch, responses)
    with pytest.raises(TooManyRedirectsError):
        transport.send("PUT", urls[0], data=b"x", guard=GUARD)
