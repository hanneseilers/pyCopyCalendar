"""Target gateway - the only component allowed to issue PUT or DELETE.

Every write goes through `client.session` (a CalendarSyncSession built
with allow_mutations=True and a real TargetContainmentGuard - see
transport.py), so a destination outside the configured target collection
is refused before any bytes leave the process, on the initial request and
on every redirect hop. This class never calls a generic caldav
persistence method (`save`/`Event.save()`); it issues PUT/DELETE directly
so ETag preconditions (NFR-004) are guaranteed to be sent rather than
hoping the library forwards them.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urljoin

import caldav

from .config import NextcloudConfig, TargetConfig
from .credentials import Credentials
from .ical import extract_managed_metadata, parse_vevents
from .models import ManagedTargetEvent, WriteResult
from .transport import (
    BasicAuth,
    CalendarSyncSession,
    MutationBlockedError,
    StdlibTransport,
    TargetContainmentGuard,
)

log = logging.getLogger("calendar_sync.target_gateway")

_ICS_CONTENT_TYPE = "text/calendar; charset=utf-8"


class TargetWriteError(Exception):
    """A target write failed for a reason other than a precondition
    conflict (see PreconditionFailed) - e.g. transport failure or an
    unexpected status code."""


class PreconditionFailed(TargetWriteError):
    """HTTP 409/412: the target resource changed since it was last read.
    Callers must re-read that one resource and re-plan it once, then
    quarantine on a second conflict (spec section 16, Phase D step 8)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def build_target_client(
    nc: NextcloudConfig, credentials: Credentials, *, guard: TargetContainmentGuard
) -> caldav.DAVClient:
    transport = StdlibTransport(
        verify_tls=nc.verify_tls,
        connect_timeout=nc.connect_timeout_seconds,
        read_timeout=nc.read_timeout_seconds,
        user_agent=nc.user_agent,
    )
    client = caldav.DAVClient(
        url=nc.base_url,
        username=credentials.username,
        password=credentials.app_password,
        ssl_verify_cert=nc.verify_tls,
        headers={"User-Agent": nc.user_agent},
        auth=BasicAuth(credentials.username, credentials.app_password),
    )
    client.session = CalendarSyncSession(transport, allow_mutations=True, guard=guard)
    return client


class TargetGateway:
    def __init__(self, target: TargetConfig, client: caldav.DAVClient, credentials: Credentials):
        self.calendar_url = target.calendar_url
        self._client = client
        self._calendar = caldav.Calendar(client=client, url=target.calendar_url)
        self._auth = BasicAuth(credentials.username, credentials.app_password)

    def preflight(self) -> bool:
        try:
            self._calendar.get_display_name()
            return True
        except Exception as exc:
            log.warning("Preflight failed for target calendar: %s", exc)
            return False

    def list_managed(self, start: datetime, end: datetime) -> list[ManagedTargetEvent]:
        objects = self._calendar.search(event=True, start=start, end=end, expand=False)
        managed: list[ManagedTargetEvent] = []
        for obj in objects:
            href = str(obj.url)
            etag = getattr(obj, "etag", None)
            try:
                vevents = parse_vevents(obj.data)
            except Exception as exc:
                log.warning("Skipping unparsable target object %s: %s", href, exc)
                continue
            entry = extract_managed_metadata(href, etag, vevents)
            if entry is not None:
                managed.append(entry)
        return managed

    def _object_href(self, target_uid: str) -> str:
        base = self.calendar_url if self.calendar_url.endswith("/") else self.calendar_url + "/"
        return urljoin(base, f"{target_uid}.ics")

    def create(self, target_uid: str, ics_bytes: bytes) -> WriteResult:
        href = self._object_href(target_uid)
        headers = {"Content-Type": _ICS_CONTENT_TYPE, "If-None-Match": "*"}
        response = self._request("PUT", href, data=ics_bytes, headers=headers)
        self._raise_for_write_status(response, href, expected=(200, 201, 204))
        return WriteResult(href=href, etag=response.headers.get("ETag"), status_code=response.status_code)

    def replace(self, href: str, etag: str | None, ics_bytes: bytes) -> WriteResult:
        headers = {"Content-Type": _ICS_CONTENT_TYPE}
        if etag:
            headers["If-Match"] = etag
        response = self._request("PUT", href, data=ics_bytes, headers=headers)
        self._raise_for_write_status(response, href, expected=(200, 201, 204))
        return WriteResult(href=href, etag=response.headers.get("ETag"), status_code=response.status_code)

    def delete(self, href: str, etag: str | None) -> None:
        headers = {"If-Match": etag} if etag else {}
        response = self._request("DELETE", href, data=None, headers=headers)
        if response.status_code == 404:
            # Only ever called after ownership was established from a
            # managed listing, so an already-absent object is convergence,
            # not a source of truth about what "should" be deleted.
            log.info("Target object %s was already gone", href)
            return
        self._raise_for_write_status(response, href, expected=(200, 202, 204))

    def _request(self, method: str, href: str, *, data: bytes | None, headers: dict):
        try:
            return self._client.session.request(method, href, data=data, headers=headers, auth=self._auth)
        except MutationBlockedError:
            raise
        except Exception as exc:
            raise TargetWriteError(f"{method} {href} failed: {exc}") from exc

    @staticmethod
    def _raise_for_write_status(response, href: str, *, expected: tuple[int, ...]) -> None:
        if response.status_code in (409, 412):
            raise PreconditionFailed(
                f"{href} changed since it was last read (HTTP {response.status_code}).",
                response.status_code,
            )
        if response.status_code not in expected:
            raise TargetWriteError(
                f"Unexpected status {response.status_code} {response.reason} writing {href}"
            )
