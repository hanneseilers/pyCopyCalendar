from calendar_sync.config import CanonicalLocation, LocationFilterConfig
from calendar_sync.location import LocationMatcher
from calendar_sync.textnorm import normalize_location


def make_matcher(case_sensitive=False):
    config = LocationFilterConfig(
        match_mode="normalized_exact",
        case_sensitive=case_sensitive,
        locations=(
            CanonicalLocation(canonical="Berlin Office", aliases=("Berlin Office", "Office Berlin", "BER Room")),
            CanonicalLocation(canonical="Hamburg Office", aliases=("Hamburg Office",)),
        ),
    )
    return LocationMatcher(config)


def test_matches_exact_alias():
    matcher = make_matcher()
    assert matcher.match("Berlin Office") == "Berlin Office"
    assert matcher.match("Office Berlin") == "Berlin Office"
    assert matcher.match("BER Room") == "Berlin Office"


def test_case_insensitive_by_default():
    matcher = make_matcher()
    assert matcher.match("berlin office") == "Berlin Office"
    assert matcher.match("BERLIN OFFICE") == "Berlin Office"


def test_case_sensitive_when_configured():
    matcher = make_matcher(case_sensitive=True)
    assert matcher.match("berlin office") is None
    assert matcher.match("Berlin Office") == "Berlin Office"


def test_no_match_for_unrelated_location():
    matcher = make_matcher()
    assert matcher.match("Somewhere Else") is None


def test_empty_or_none_location_is_ineligible():
    matcher = make_matcher()
    assert matcher.match(None) is None
    assert matcher.match("") is None
    assert matcher.match("   ") is None
    assert matcher.is_eligible(None) is False


def test_whitespace_and_unicode_normalization():
    matcher = make_matcher()
    # NFKC normalizes full-width/compatibility forms; internal whitespace collapses.
    assert matcher.match("Berlin   Office") == "Berlin Office"
    assert matcher.match("  Berlin Office  ") == "Berlin Office"


def test_normalize_location_nfkc_and_casefold():
    # U+FB00 LATIN SMALL LIGATURE FF normalizes (NFKC) to "ff"
    assert normalize_location("ﬀice Berlin", case_sensitive=False) == "ffice berlin"
    assert normalize_location(None, case_sensitive=False) == ""
