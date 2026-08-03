import pytest

from src.media.normalize import media_key


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Query strings are the common CDN variant: same asset, different sizing params.
        ("https://i.example.com/a.jpg?width=640&s=abc", "https://i.example.com/a.jpg"),
        ("https://i.example.com/a.jpg#frag", "https://i.example.com/a.jpg"),
        ("https://i.example.com/a.jpg?w=1#frag", "https://i.example.com/a.jpg"),
        # Host casing and www. are cosmetic.
        ("https://I.Example.COM/a.jpg", "https://i.example.com/a.jpg"),
        ("https://www.example.com/a.jpg", "https://example.com/a.jpg"),
        ("HTTPS://example.com/a.jpg", "https://example.com/a.jpg"),
        # Path casing is NOT cosmetic — CDN asset IDs are case-sensitive.
        ("https://example.com/AbC.jpg", "https://example.com/AbC.jpg"),
        # Already canonical: unchanged.
        ("https://example.com/a.jpg", "https://example.com/a.jpg"),
    ],
)
def test_media_key_normalises(url: str, expected: str) -> None:
    assert media_key(url) == expected


def test_media_key_collapses_query_variants_of_the_same_asset() -> None:
    a = media_key("https://cdn.example.com/pic.jpg?width=320&auto=webp")
    b = media_key("https://cdn.example.com/pic.jpg?width=1080&auto=webp&s=deadbeef")
    assert a == b


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Different asset IDs must never collapse.
        ("https://example.com/a.jpg", "https://example.com/b.jpg"),
        # Different hosts must never collapse — this is exactly the mistake a
        # speculative preview-host rewrite rule would make.
        ("https://a.example.com/x.jpg", "https://b.example.com/x.jpg"),
        # www. stripping must not merge distinct subdomains.
        ("https://www.example.com/x.jpg", "https://img.example.com/x.jpg"),
        # Case-sensitive path IDs stay distinct.
        ("https://example.com/AbC.jpg", "https://example.com/abc.jpg"),
    ],
)
def test_media_key_keeps_distinct_urls_distinct(a: str, b: str) -> None:
    assert media_key(a) != media_key(b)


@pytest.mark.parametrize("url", ["", "not a url", "/relative/path.jpg", "data:image/png;base64,AAAA"])
def test_media_key_passes_through_non_urls(url: str) -> None:
    """Input without a scheme+host is returned verbatim, so malformed URLs get
    a stable key of their own rather than all colliding on one empty key."""
    assert media_key(url) == url
