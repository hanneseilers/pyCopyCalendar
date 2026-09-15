import pytest

from calendar_sync.urlsafety import UrlSafetyError, canonicalize_url


def test_canonicalizes_scheme_host_port_path():
    c = canonicalize_url("https://Cloud.Example.invalid/remote.php/dav/calendars/user/a")
    assert c.scheme == "https"
    assert c.host == "cloud.example.invalid"
    assert c.port == 443
    assert c.path == "/remote.php/dav/calendars/user/a/"
    assert c.origin == "https://cloud.example.invalid:443"


def test_collapses_double_slashes():
    c = canonicalize_url("https://cloud.example.invalid/remote.php//dav//calendars/")
    assert c.path == "/remote.php/dav/calendars/"


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud.example.invalid/dav/",  # plaintext without opt-in
        "ftp://cloud.example.invalid/dav/",
        "https://user:pass@cloud.example.invalid/dav/",
        "https://cloud.example.invalid/dav/#fragment",
        "https://cloud.example.invalid/dav/?query=1",
        "https://cloud.example.invalid/dav/../../etc/",
        "https://cloud.example.invalid/dav/./x/",
        "https://",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(UrlSafetyError):
        canonicalize_url(url)


def test_allows_plaintext_only_when_opted_in():
    c = canonicalize_url("http://localhost/dav/", allow_insecure=True)
    assert c.scheme == "http"


def test_is_within():
    base = canonicalize_url("https://cloud.example.invalid/remote.php/dav/")
    child = canonicalize_url("https://cloud.example.invalid/remote.php/dav/calendars/user/a/")
    foreign = canonicalize_url("https://other.invalid/remote.php/dav/")
    assert child.is_within(base)
    assert not foreign.is_within(base)


def test_is_direct_child_of():
    collection = canonicalize_url("https://cloud.example.invalid/remote.php/dav/calendars/user/target/")
    direct_child = canonicalize_url(
        "https://cloud.example.invalid/remote.php/dav/calendars/user/target/event.ics"
    )
    nested = canonicalize_url(
        "https://cloud.example.invalid/remote.php/dav/calendars/user/target/sub/event.ics"
    )
    assert direct_child.is_direct_child_of(collection)
    assert not nested.is_direct_child_of(collection)
    assert not collection.is_direct_child_of(collection)  # the collection itself is not a child of itself
