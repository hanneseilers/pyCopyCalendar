"""Stdlib-only HTTP transport and the target write containment guard.

Two things live here on purpose:

1. A `http.client` + `ssl` transport instead of `requests`/`niquests`.
   Some hosting providers put a firewall in front of Nextcloud that
   fingerprints and blocks the TLS/HTTP handshake produced by both of
   those libraries while a plain stdlib connection (and `curl`) gets
   through untouched - this was diagnosed against IONOS-hosted Nextcloud
   behind STRATO shared webspace in the predecessor of this project, and
   the spec targets the exact same hosting combination. A custom
   `User-Agent` (`nextcloud.user_agent` in config) is part of the same
   workaround, since some deployments also filter on that header.

2. `TargetContainmentGuard`, the one place every mutating DAV request
   (`PUT`/`POST`/`PATCH`/`DELETE`) must pass through, per
   TECHNICAL_SPECIFICATION.md section 8. `CalendarSyncSession` refuses to
   send a mutating request at all unless it was constructed with
   `allow_mutations=True` (only the target gateway does this) and the
   guard accepts the destination - including after following a redirect.
"""

from __future__ import annotations

import http.client
import logging
import random
import ssl
import time
from collections import namedtuple
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urlsplit

from .urlsafety import CanonicalUrl, UrlSafetyError, canonicalize_url

log = logging.getLogger("calendar_sync.transport")

SAFE_RETRYABLE_METHODS = frozenset({"GET", "PROPFIND", "REPORT"})
MUTATING_METHODS = frozenset({"PUT", "POST", "PATCH", "DELETE"})
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
MAX_REDIRECTS = 5

BasicAuth = namedtuple("BasicAuth", ["username", "password"])


class TransportError(Exception):
    """Base class for transport-level failures (never for HTTP error status codes,
    which callers inspect on the returned response)."""


class MutationBlockedError(TransportError):
    """A mutating request was refused by the containment guard or by a
    session with allow_mutations=False. This must never be silently
    downgraded to a skip - it means the code tried to write somewhere it
    must not."""


class TooManyRedirectsError(TransportError):
    pass


class Response:
    """Just enough of requests.Response/niquests.Response for
    caldav.davclient.DAVResponse to read across caldav versions."""

    def __init__(self, status_code: int, reason: str, headers: Message, content: bytes):
        self.status_code = status_code
        self.reason = reason
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class TargetContainmentGuard:
    """Validates that a mutating request's destination is a direct child
    of the exact configured target collection, on the configured origin,
    and not equal to or below any configured source collection."""

    canonical_target: CanonicalUrl
    canonical_sources: tuple[CanonicalUrl, ...]
    allow_insecure_for_testing: bool = False

    def validate(self, url: str) -> CanonicalUrl:
        try:
            candidate = canonicalize_url(url, allow_insecure=self.allow_insecure_for_testing)
        except UrlSafetyError as exc:
            raise MutationBlockedError(f"Refusing to write to unsafe URL: {exc}") from exc

        for source in self.canonical_sources:
            if candidate.is_within(source):
                raise MutationBlockedError(
                    f"Refusing to write to {url!r}: it is at or below a source collection."
                )
        if not candidate.is_direct_child_of(self.canonical_target):
            raise MutationBlockedError(
                f"Refusing to write to {url!r}: it is not a direct child of the "
                "configured target collection."
            )
        return candidate


def _retry_after_seconds(headers: Message) -> float | None:
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is rare for CalDAV servers; ignore rather than misparse.


class StdlibTransport:
    """Sends one HTTP request via `http.client` + `ssl`, with manual bounded
    redirect handling. No retry logic here - see `CalendarSyncSession` for
    the safe-method retry wrapper, kept separate because retry policy
    differs between read and write verbs.
    """

    def __init__(
        self,
        *,
        verify_tls: bool,
        connect_timeout: float,
        read_timeout: float,
        user_agent: str,
    ):
        self._verify_tls = verify_tls
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._user_agent = user_agent

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not self._verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _send_once(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None,
        headers: dict,
        auth: BasicAuth | None,
    ) -> tuple[Response, str]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" and self._verify_tls:
            raise TransportError(f"Refusing plaintext HTTP request with TLS verification enabled: {url!r}")

        send_headers = dict(headers)
        send_headers.setdefault("User-Agent", self._user_agent)
        if auth is not None:
            import base64

            token = base64.b64encode(f"{auth.username}:{auth.password}".encode("utf-8")).decode("ascii")
            send_headers["Authorization"] = f"Basic {token}"

        body = data
        if isinstance(body, str):
            body = body.encode("utf-8")
        if isinstance(body, bytes):
            # caldav's XML body builder (and our own ICS bodies) may contain
            # literal CRLF; some hosting firewalls flag embedded \r\n inside
            # a request body as a CRLF-injection/request-smuggling attempt
            # and block the request outright. Irrelevant to XML/ICS parsing
            # either way, so normalize it away.
            body = body.replace(b"\r\n", b"\n")

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        timeout = self._connect_timeout + self._read_timeout
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=timeout, context=self._ssl_context()
            )
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=send_headers)
            resp = conn.getresponse()
            content = resp.read()
            return Response(resp.status, resp.reason, resp.headers, content), url
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"{method} {url} failed: {exc}") from exc
        finally:
            conn.close()

    def send(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict | None = None,
        auth: BasicAuth | None = None,
        guard: TargetContainmentGuard | None = None,
    ) -> Response:
        """Send one request, following same-collection redirects manually.

        If `guard` is given, both the original URL (for mutating methods)
        and every redirect hop are re-validated against it before the
        request is sent - a redirect must never be able to smuggle a
        mutation outside the permitted target collection.
        """
        current_url = url
        current_method = method
        current_data = data
        for _ in range(MAX_REDIRECTS + 1):
            if guard is not None and current_method in MUTATING_METHODS:
                guard.validate(current_url)
            response, _ = self._send_once(
                current_method, current_url, data=current_data, headers=headers or {}, auth=auth
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    return response
                next_url = _resolve_redirect(current_url, location)
                log.info("Following redirect %s -> %s", current_url, next_url)
                if response.status_code == 303:
                    current_method, current_data = "GET", None
                current_url = next_url
                continue
            return response
        raise TooManyRedirectsError(f"Exceeded {MAX_REDIRECTS} redirects starting at {url!r}")


def _resolve_redirect(base_url: str, location: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, location)


class CalendarSyncSession:
    """Drop-in replacement for the requests.Session/niquests.Session that
    caldav.DAVClient normally builds internally. Implements the same
    `.request(...)` signature so it can be assigned to `client.session`.

    `allow_mutations=False` (used for every source gateway's client)
    refuses any PUT/POST/PATCH/DELETE outright, regardless of `guard` -
    the source gateway must never be able to write even if calling code
    has a bug. The target gateway's client uses `allow_mutations=True`
    together with a real `TargetContainmentGuard`.
    """

    def __init__(
        self,
        transport: StdlibTransport,
        *,
        allow_mutations: bool,
        guard: TargetContainmentGuard | None = None,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
    ):
        if allow_mutations and guard is None:
            raise ValueError("A session with allow_mutations=True requires a TargetContainmentGuard.")
        self._transport = transport
        self._allow_mutations = allow_mutations
        self._guard = guard if allow_mutations else None
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay

    def request(
        self,
        method: str,
        url: str,
        data=None,
        headers=None,
        proxies=None,  # unused; accepted for caldav.DAVClient compatibility
        auth=None,
        timeout=None,  # unused; StdlibTransport uses the configured connect/read timeouts
        verify=True,  # unused; StdlibTransport uses the configured verify_tls
        cert=None,  # unused; client certificates are not supported
    ) -> Response:
        method = method.upper()
        if method in MUTATING_METHODS and not self._allow_mutations:
            raise MutationBlockedError(
                f"Refusing {method} {url!r}: this session is read-only (source gateway)."
            )

        basic_auth = None
        if auth is not None:
            username = getattr(auth, "username", None)
            password = getattr(auth, "password", None)
            if username is not None:
                basic_auth = BasicAuth(username, password)

        if method not in SAFE_RETRYABLE_METHODS:
            return self._transport.send(
                method, url, data=data, headers=headers, auth=basic_auth, guard=self._guard
            )
        return self._send_with_retry(method, url, data=data, headers=headers, auth=basic_auth)

    def _send_with_retry(self, method, url, *, data, headers, auth) -> Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._transport.send(method, url, data=data, headers=headers, auth=auth)
            except TransportError:
                if attempt >= self._max_attempts:
                    raise
                self._sleep_backoff(attempt, retry_after=None)
                continue

            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_attempts:
                self._sleep_backoff(attempt, retry_after=_retry_after_seconds(response.headers))
                continue
            return response

    def _sleep_backoff(self, attempt: int, *, retry_after: float | None) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(self._max_delay, self._base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.5 + random.random())  # full jitter around the exponential value
        log.info("Retrying after %.2fs (attempt %d)", delay, attempt)
        time.sleep(delay)
