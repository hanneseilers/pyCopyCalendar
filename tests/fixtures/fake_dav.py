"""In-process fake CalDAV server used as a fake HTTP transport for tests.

Implements just enough of PROPFIND/REPORT calendar-query/GET/PUT/DELETE
against one or more in-memory calendar collections to exercise the real
`caldav` library's request building and response parsing, and our own
CalendarSyncSession (retry, containment guard) - without any network
access. See CODING_AGENT_PROMPT.md's "Use fake HTTP transports and
fixtures for automated tests. Do not contact a real Nextcloud instance."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import Message
from urllib.parse import urlsplit

from calendar_sync.transport import Response


@dataclass
class FakeObject:
    ics_text: str
    etag: str


@dataclass
class FakeCollection:
    href: str
    display_name: str
    objects: dict[str, FakeObject] = field(default_factory=dict)  # href -> object
    _etag_counter: int = 0

    def next_etag(self) -> str:
        self._etag_counter += 1
        return f'"etag-{self._etag_counter}"'


def _headers(extra: dict | None = None) -> Message:
    msg = Message()
    for k, v in (extra or {}).items():
        msg[k] = v
    return msg


class FakeDavTransport:
    """Drop-in replacement for StdlibTransport: same `.send(...)` signature,
    routes everything to in-memory FakeCollection objects instead of a
    socket. `origin` must match the canonical origin used in test config
    (e.g. "https://cloud.example.invalid:443")."""

    def __init__(self, origin: str = "https://cloud.example.invalid:443"):
        self.origin = origin
        self.collections: dict[str, FakeCollection] = {}  # path -> collection
        self.requests: list[tuple[str, str]] = []  # (method, url) audit log for tests

    def add_collection(self, path: str, display_name: str) -> FakeCollection:
        if not path.endswith("/"):
            path += "/"
        collection = FakeCollection(href=path, display_name=display_name)
        self.collections[path] = collection
        return collection

    def _find_collection(self, path: str) -> FakeCollection | None:
        for collection_path, collection in self.collections.items():
            if path == collection_path or (path.startswith(collection_path) and path != collection_path):
                if path == collection_path:
                    return collection
        return self.collections.get(path)

    def _find_object(self, path: str) -> tuple[FakeCollection, str] | tuple[None, None]:
        for collection_path, collection in self.collections.items():
            if path.startswith(collection_path) and path in collection.objects:
                return collection, path
        return None, None

    def send(self, method, url, *, data=None, headers=None, auth=None, guard=None) -> Response:
        self.requests.append((method, url))
        if guard is not None and method in ("PUT", "POST", "PATCH", "DELETE"):
            guard.validate(url)  # raises MutationBlockedError on violation, exactly like the real transport

        parsed = urlsplit(url)
        path = parsed.path

        if method == "PROPFIND":
            return self._propfind(path)
        if method == "REPORT":
            return self._report(path)
        if method == "GET":
            return self._get(path)
        if method == "PUT":
            return self._put(path, data, headers or {})
        if method == "DELETE":
            return self._delete(path, headers or {})
        raise AssertionError(f"FakeDavTransport does not implement method {method!r}")

    def _propfind(self, path: str) -> Response:
        collection = self._find_collection(path)
        if collection is None:
            return Response(404, "Not Found", _headers(), b"")
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>{collection.href}</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>{collection.display_name}</d:displayname>
        <d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>
        <cal:supported-calendar-component-set>
          <cal:comp name="VEVENT"/>
        </cal:supported-calendar-component-set>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""
        return Response(207, "Multi-Status", _headers({"Content-Type": "application/xml"}), body.encode())

    def _report(self, path: str) -> Response:
        collection = self._find_collection(path)
        if collection is None:
            return Response(404, "Not Found", _headers(), b"")
        responses = []
        for href, obj in collection.objects.items():
            escaped = obj.ics_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            responses.append(f"""
  <d:response>
    <d:href>{href}</d:href>
    <d:propstat>
      <d:prop>
        <d:getetag>{obj.etag}</d:getetag>
        <cal:calendar-data>{escaped}</cal:calendar-data>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>""")
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">{''.join(responses)}
</d:multistatus>"""
        return Response(207, "Multi-Status", _headers({"Content-Type": "application/xml"}), body.encode())

    def _get(self, path: str) -> Response:
        collection, href = self._find_object(path)
        if collection is None:
            return Response(404, "Not Found", _headers(), b"Not Found")
        obj = collection.objects[href]
        return Response(
            200, "OK", _headers({"ETag": obj.etag, "Content-Type": "text/calendar"}), obj.ics_text.encode()
        )

    def _put(self, path: str, data: bytes, headers: dict) -> Response:
        collection = None
        for collection_path, c in self.collections.items():
            if path.startswith(collection_path):
                collection = c
                break
        if collection is None:
            return Response(404, "Not Found", _headers(), b"")

        existing = collection.objects.get(path)
        if_none_match = headers.get("If-None-Match")
        if_match = headers.get("If-Match")
        if if_none_match == "*" and existing is not None:
            return Response(412, "Precondition Failed", _headers(), b"")
        if if_match is not None and (existing is None or existing.etag != if_match):
            return Response(412, "Precondition Failed", _headers(), b"")

        etag = collection.next_etag()
        ics_text = data.decode("utf-8") if isinstance(data, bytes) else data
        collection.objects[path] = FakeObject(ics_text=ics_text, etag=etag)
        status = 204 if existing is not None else 201
        return Response(status, "Created" if status == 201 else "No Content", _headers({"ETag": etag}), b"")

    def _delete(self, path: str, headers: dict) -> Response:
        collection, href = self._find_object(path)
        if collection is None:
            return Response(404, "Not Found", _headers(), b"")
        obj = collection.objects[href]
        if_match = headers.get("If-Match")
        if if_match is not None and obj.etag != if_match:
            return Response(412, "Precondition Failed", _headers(), b"")
        del collection.objects[href]
        return Response(204, "No Content", _headers(), b"")
