from pathlib import Path

import pytest

from src.feeds.opml import parse_opml

_VALID_OPML = """\
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="Feed One" xmlUrl="https://example.com/feed1.xml"/>
    <outline type="rss" text="Feed Two" xmlUrl="https://example.com/feed2.xml"/>
  </body>
</opml>"""


def test_parse_valid_opml(tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(_VALID_OPML)
    feeds = parse_opml(str(f))
    assert len(feeds) == 2
    assert feeds[0]["url"] == "https://example.com/feed1.xml"
    assert feeds[0]["title"] == "Feed One"
    assert feeds[1]["url"] == "https://example.com/feed2.xml"


def test_parse_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        parse_opml("/nonexistent/path/feeds.opml")


def test_parse_empty_opml(tmp_path: Path) -> None:
    f = tmp_path / "feeds.opml"
    f.write_text(
        '<?xml version="1.0"?><opml version="2.0"><head/><body/></opml>'
    )
    feeds = parse_opml(str(f))
    assert feeds == []


def test_parse_uses_url_as_fallback_title(tmp_path: Path) -> None:
    opml = """\
<?xml version="1.0"?>
<opml version="2.0"><head/><body>
  <outline type="rss" xmlUrl="https://example.com/no-title.xml"/>
</body></opml>"""
    f = tmp_path / "feeds.opml"
    f.write_text(opml)
    feeds = parse_opml(str(f))
    assert feeds[0]["title"] == "https://example.com/no-title.xml"


def test_parse_malformed_opml(tmp_path: Path) -> None:
    f = tmp_path / "bad.opml"
    f.write_text("<<<not valid xml>>>")
    feeds = parse_opml(str(f))
    assert feeds == []
