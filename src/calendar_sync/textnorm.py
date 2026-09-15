"""Shared text normalization for LOCATION/alias comparison.

Split out from location.py and config.py so both the alias-collision
check performed at config-load time and the actual per-event matcher
built by LocationMatcher use exactly one normalization rule - a
divergence between the two would be a silent correctness bug.
"""

from __future__ import annotations

import unicodedata


def normalize_location(value: str | None, *, case_sensitive: bool) -> str:
    """Canonicalize a LOCATION-like string for comparison.

    Applies Unicode NFKC normalization, trims and collapses internal
    whitespace, and (unless case-sensitive matching is configured) applies
    Unicode case folding.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip()
    text = " ".join(text.split())
    if not case_sensitive:
        text = text.casefold()
    return text
