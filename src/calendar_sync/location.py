"""LOCATION normalization and alias matching.

Filtering happens locally, after retrieving all relevant source events -
never via a server-side text search - because eligibility *transitions*
(an occurrence entering or leaving the allowed set) must be detected, not
just the current match. See TECHNICAL_SPECIFICATION.md section 10.

Only `normalized_exact` matching is supported: the complete normalized
LOCATION must equal a normalized alias. Substring/regex matching was
deliberately excluded to avoid false positives that would silently mirror
(or fail to delete) the wrong events.
"""

from __future__ import annotations

from .config import LocationFilterConfig
from .textnorm import normalize_location

__all__ = ["normalize_location", "LocationMatcher"]


class LocationMatcher:
    """Resolves an effective LOCATION to a canonical location name, or
    `None` if the location is absent, empty, or not in the alias set."""

    def __init__(self, config: LocationFilterConfig):
        self._case_sensitive = config.case_sensitive
        self._alias_to_canonical: dict[str, str] = {}
        for location in config.locations:
            for alias in location.aliases:
                normalized = normalize_location(alias, case_sensitive=self._case_sensitive)
                self._alias_to_canonical[normalized] = location.canonical

    def match(self, location: str | None) -> str | None:
        normalized = normalize_location(location, case_sensitive=self._case_sensitive)
        if not normalized:
            return None
        return self._alias_to_canonical.get(normalized)

    def is_eligible(self, location: str | None) -> bool:
        return self.match(location) is not None
