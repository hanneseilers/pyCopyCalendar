"""Read-only source gateway.

Exposes only discovery/read operations - no save/put/create/update/delete
- per TECHNICAL_SPECIFICATION.md section 8. A caldav calendar-object
returned here must never be passed to that library's own persistence
methods. As defense in depth, the underlying `client.session` is a
CalendarSyncSession built with allow_mutations=False (see transport.py),
so even a bug that tried to call a mutation would be refused at the
transport layer.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

import caldav

from .config import NextcloudConfig, SourceConfig
from .credentials import Credentials
from .models import CalendarResource, SourceCapabilities, normalize_etag
from .transport import BasicAuth, CalendarSyncSession, StdlibTransport

log = logging.getLogger("calendar_sync.source_gateway")


class SourceReadError(Exception):
    """A source could not be read authoritatively. Callers must treat this
    as "abort, perform no target mutations" - never as an empty calendar
    (NFR-012)."""


class ReadOnlySourceGatewayProtocol(Protocol):
    def preflight(self) -> SourceCapabilities: ...

    def fetch_calendar_objects(self, start: datetime, end: datetime) -> list[CalendarResource]: ...

    def get_resource(self, href: str) -> CalendarResource: ...


def build_source_client(nc: NextcloudConfig, credentials: Credentials) -> caldav.DAVClient:
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
    client.session = CalendarSyncSession(transport, allow_mutations=False)
    return client


class ReadOnlySourceGateway:
    def __init__(self, source: SourceConfig, client: caldav.DAVClient):
        self.source_id = source.id
        self.calendar_url = source.calendar_url
        self._calendar = caldav.Calendar(client=client, url=source.calendar_url)

    def preflight(self) -> SourceCapabilities:
        try:
            display_name = self._calendar.get_display_name()
            return SourceCapabilities(reachable=True, display_name=display_name)
        except Exception as exc:
            log.warning("Preflight failed for source '%s': %s", self.source_id, exc)
            return SourceCapabilities(reachable=False, display_name=None)

    def fetch_calendar_objects(self, start: datetime, end: datetime) -> list[CalendarResource]:
        """Read every calendar object overlapping [start, end) from this
        source. Raises SourceReadError on any failure or non-authoritative
        result instead of returning a partial/empty list."""
        try:
            objects = self._calendar.search(event=True, start=start, end=end, expand=False)
        except Exception as exc:
            raise SourceReadError(f"Failed to query source '{self.source_id}': {exc}") from exc

        resources: list[CalendarResource] = []
        for obj in objects:
            try:
                ics_text = obj.data
            except Exception as exc:
                raise SourceReadError(
                    f"Failed to read a calendar object body from source '{self.source_id}': {exc}"
                ) from exc
            if not ics_text or not ics_text.strip():
                raise SourceReadError(
                    f"Source '{self.source_id}' returned an empty/truncated calendar object at "
                    f"{getattr(obj, 'url', '?')!r}; refusing to treat this as an empty calendar."
                )
            resources.append(
                CalendarResource(
                    href=str(obj.url), etag=normalize_etag(getattr(obj, "etag", None)), ics_text=ics_text
                )
            )
        return resources

    def get_resource(self, href: str) -> CalendarResource:
        try:
            obj = self._calendar.object_by_url(href)
            return CalendarResource(href=href, etag=normalize_etag(getattr(obj, "etag", None)), ics_text=obj.data)
        except Exception as exc:
            raise SourceReadError(
                f"Failed to read '{href}' from source '{self.source_id}': {exc}"
            ) from exc
