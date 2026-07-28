from src.media.detector import detect_all_media, detect_media, detect_type


def test_detect_type_jpeg() -> None:
    assert detect_type("https://example.com/photo.jpg") == "image"


def test_detect_type_png() -> None:
    assert detect_type("https://example.com/photo.png") == "image"


def test_detect_type_gif() -> None:
    assert detect_type("https://example.com/anim.gif") == "gif"


def test_detect_type_mp4() -> None:
    assert detect_type("https://example.com/clip.mp4") == "video"


def test_detect_type_webm() -> None:
    assert detect_type("https://example.com/clip.webm") == "video"


def test_detect_type_unknown_returns_none() -> None:
    assert detect_type("https://example.com/doc.pdf") is None


def test_detect_type_strips_query_string() -> None:
    assert detect_type("https://example.com/photo.jpg?v=1") == "image"


def test_detect_media_from_enclosure() -> None:
    entry = {"enclosures": [{"url": "https://example.com/photo.jpg", "type": "image/jpeg"}]}
    assert detect_media(entry) == ("https://example.com/photo.jpg", "image")


def test_detect_media_from_media_content() -> None:
    entry = {
        "enclosures": [],
        "media_content": [{"url": "https://example.com/anim.gif"}],
    }
    assert detect_media(entry) == ("https://example.com/anim.gif", "gif")


def test_detect_media_from_media_thumbnail() -> None:
    entry = {
        "enclosures": [],
        "media_content": [],
        "media_thumbnail": [{"url": "https://example.com/thumb.jpg"}],
    }
    assert detect_media(entry) == ("https://example.com/thumb.jpg", "image")


def test_detect_media_from_og_image() -> None:
    entry = {
        "enclosures": [],
        "media_content": [],
        "media_thumbnail": [],
        "summary": '<meta property="og:image" content="https://example.com/og.png"/>',
    }
    assert detect_media(entry) == ("https://example.com/og.png", "image")


def test_detect_media_returns_none_when_no_media() -> None:
    entry = {"enclosures": [], "summary": "<p>Text only</p>"}
    assert detect_media(entry) is None


def test_detect_media_enclosure_takes_priority_over_media_content() -> None:
    entry = {
        "enclosures": [{"url": "https://example.com/enc.jpg"}],
        "media_content": [{"url": "https://example.com/mc.gif"}],
    }
    url, _ = detect_media(entry)
    assert url == "https://example.com/enc.jpg"


def test_detect_all_media_multi_enclosure_gallery() -> None:
    entry = {
        "enclosures": [
            {"url": "https://example.com/a.jpg"},
            {"url": "https://example.com/b.gif"},
            {"url": "https://example.com/c.mp4"},
        ]
    }
    assert detect_all_media(entry) == [
        ("https://example.com/a.jpg", "image"),
        ("https://example.com/b.gif", "gif"),
        ("https://example.com/c.mp4", "video"),
    ]


def test_detect_all_media_combines_enclosures_and_media_content_deduped() -> None:
    entry = {
        "enclosures": [{"url": "https://example.com/a.jpg"}],
        "media_content": [
            {"url": "https://example.com/a.jpg"},  # duplicate of the enclosure
            {"url": "https://example.com/b.jpg"},
        ],
    }
    assert detect_all_media(entry) == [
        ("https://example.com/a.jpg", "image"),
        ("https://example.com/b.jpg", "image"),
    ]


def test_detect_all_media_skips_unsupported_extensions() -> None:
    entry = {
        "enclosures": [
            {"url": "https://example.com/doc.pdf"},
            {"url": "https://example.com/ok.png"},
        ]
    }
    assert detect_all_media(entry) == [("https://example.com/ok.png", "image")]


def test_detect_all_media_thumbnail_fallback_returns_single() -> None:
    entry = {"media_thumbnail": [{"url": "https://example.com/thumb.jpg"}]}
    assert detect_all_media(entry) == [("https://example.com/thumb.jpg", "image")]


def test_detect_all_media_og_image_fallback_returns_single() -> None:
    entry = {"summary": '<meta property="og:image" content="https://example.com/og.png"/>'}
    assert detect_all_media(entry) == [("https://example.com/og.png", "image")]


def test_detect_all_media_returns_empty_when_no_media() -> None:
    assert detect_all_media({"summary": "<p>text only</p>"}) == []
