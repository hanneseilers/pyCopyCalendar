"""Shared URL canonicalization used both by config validation (startup
source/target separation) and by the target containment guard (per-request,
see transport.py). Keeping one implementation avoids the two checks
silently drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


class UrlSafetyError(ValueError):
    """A configured or requested URL fails canonicalization or containment."""


@dataclass(frozen=True)
class CanonicalUrl:
    scheme: str
    host: str
    port: int
    path: str  # normalized, always starts and ends with "/"
    origin: str  # scheme://host:port

    def is_within(self, other: "CanonicalUrl") -> bool:
        """True if this URL's origin matches `other` and its path is at or
        below `other`'s collection path."""
        return self.origin == other.origin and self.path.startswith(other.path)

    def is_direct_child_of(self, collection: "CanonicalUrl") -> bool:
        """True if this URL names an object directly inside `collection`
        (one path segment below it, no further nesting)."""
        if self.origin != collection.origin or not self.path.startswith(collection.path):
            return False
        remainder = self.path[len(collection.path):]
        segments = [s for s in remainder.split("/") if s]
        return len(segments) == 1


_DEFAULT_PORTS = {"https": 443, "http": 80}


def canonicalize_url(url: str, *, allow_insecure: bool = False) -> CanonicalUrl:
    parts = urlsplit(url)

    if parts.scheme not in ("https", "http"):
        raise UrlSafetyError(f"Unsupported URL scheme: {url!r}")
    if parts.scheme == "http" and not allow_insecure:
        raise UrlSafetyError(f"URL must use HTTPS: {url!r}")
    if parts.username or parts.password:
        raise UrlSafetyError(f"URL must not carry embedded user info: {url!r}")
    if parts.fragment:
        raise UrlSafetyError(f"URL must not carry a fragment: {url!r}")
    if parts.query:
        raise UrlSafetyError(f"URL must not carry a query string: {url!r}")
    if not parts.hostname:
        raise UrlSafetyError(f"URL is missing a host: {url!r}")

    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    # Collapse "//" and reject any residual ".."/"." segment - urlsplit does
    # not normalize these, and a raw ".." must never reach a DAV request.
    segments = path.split("/")
    if any(segment in ("..", ".") for segment in segments):
        raise UrlSafetyError(f"URL path must not contain '.' or '..' segments: {url!r}")
    path = "/".join(segment for segment in segments if segment != "")
    path = "/" + path + ("/" if path else "")

    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    host = parts.hostname.lower()
    origin = f"{parts.scheme}://{host}:{port}"
    return CanonicalUrl(scheme=parts.scheme, host=host, port=port, path=path, origin=origin)
