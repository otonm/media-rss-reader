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


def test_detect_all_media_description_imgs_extend_enclosure_gallery() -> None:
    entry = {
        "enclosures": [{"url": "https://i.redd.it/slide1.jpg"}],
        "summary": (
            '<img src="https://i.redd.it/slide2.jpg">'
            '<img src="https://i.redd.it/slide3.gif">'
            '<img src="https://i.redd.it/slide4.mp4">'
            '<img src="https://i.redd.it/slide5.jpg">'
        ),
    }
    assert detect_all_media(entry) == [
        ("https://i.redd.it/slide1.jpg", "image"),
        ("https://i.redd.it/slide2.jpg", "image"),
        ("https://i.redd.it/slide3.gif", "gif"),
        ("https://i.redd.it/slide4.mp4", "video"),
        ("https://i.redd.it/slide5.jpg", "image"),
    ]


def test_detect_all_media_dedupes_enclosure_against_description() -> None:
    entry = {
        "enclosures": [{"url": "https://i.redd.it/slide1.jpg"}],
        "summary": (
            '<img src="https://i.redd.it/slide1.jpg">'  # duplicate of enclosure
            '<img src="https://i.redd.it/slide2.jpg">'
            '<img src="https://i.redd.it/slide3.jpg">'
        ),
    }
    assert detect_all_media(entry) == [
        ("https://i.redd.it/slide1.jpg", "image"),
        ("https://i.redd.it/slide2.jpg", "image"),
        ("https://i.redd.it/slide3.jpg", "image"),
    ]


def test_detect_all_media_unescapes_entity_encoded_description() -> None:
    entry = {
        "enclosures": [{"url": "https://example.com/a.jpg"}],
        "summary": '&lt;img src="https://i.redd.it/x.jpg"&gt;',
    }
    assert detect_all_media(entry) == [
        ("https://example.com/a.jpg", "image"),
        ("https://i.redd.it/x.jpg", "image"),
    ]


def test_detect_all_media_skips_unsupported_ext_in_description() -> None:
    entry = {
        "enclosures": [{"url": "https://example.com/a.jpg"}],
        "summary": ('<img src="https://example.com/doc.pdf"><img src="https://example.com/b.jpg">'),
    }
    assert detect_all_media(entry) == [
        ("https://example.com/a.jpg", "image"),
        ("https://example.com/b.jpg", "image"),
    ]


def test_detect_all_media_description_only_not_promoted_to_gallery() -> None:
    """Entry with no structured media stays single-slide even with multiple imgs."""
    entry = {
        "summary": (
            '<img src="https://example.com/x.jpg">'
            '<img src="https://example.com/y.jpg">'
            '<img src="https://example.com/z.jpg">'
        ),
    }
    assert detect_all_media(entry) == []


def test_extract_img_srcs_helper() -> None:
    from src.media.detector import _extract_img_srcs

    assert _extract_img_srcs("") == []
    assert _extract_img_srcs('<img src="https://i.redd.it/a.jpg"><img src="https://i.redd.it/b.jpg">') == [
        "https://i.redd.it/a.jpg",
        "https://i.redd.it/b.jpg",
    ]
    assert _extract_img_srcs('<img src="https://i.redd.it/a.jpg"><img src="https://i.redd.it/a.jpg">') == [
        "https://i.redd.it/a.jpg",
    ]
    assert _extract_img_srcs('&lt;img src="https://i.redd.it/x.jpg"&gt;') == [
        "https://i.redd.it/x.jpg",
    ]


def test_svg_is_not_a_media_type() -> None:
    """R8: .svg URLs must not enter the DB — nothing downstream may serve one."""
    assert detect_type("http://example.com/logo.svg") is None
