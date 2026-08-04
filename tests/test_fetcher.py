import json

import httpx
import respx

from src.feeds.fetcher import _feed_id, _item_id, fetch_feed  # noqa: F401

_RSS = """\
<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Image Item</title>
      <guid>guid-img-1</guid>
      <enclosure url="https://example.com/photo.jpg" type="image/jpeg" length="0"/>
    </item>
    <item>
      <title>Text Only</title>
      <guid>guid-text-1</guid>
      <description>no media</description>
    </item>
    <item>
      <title>GIF Item</title>
      <guid>guid-gif-1</guid>
      <enclosure url="https://example.com/anim.gif" type="image/gif" length="0"/>
    </item>
  </channel>
</rss>"""


async def test_fetch_feed_returns_only_media_items(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    assert len(items) == 2
    urls = [i["media_url"] for i in items]
    assert "https://example.com/photo.jpg" in urls
    assert "https://example.com/anim.gif" in urls


async def test_fetch_feed_item_has_correct_fields(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    img = next(i for i in items if i["media_type"] == "image")
    assert img["media_url"] == "https://example.com/photo.jpg"
    assert img["feed_id"] == _feed_id("https://example.com/feed.xml")
    assert img["guid"] == "guid-img-1"
    assert "id" in img


async def test_fetch_feed_same_guid_produces_same_id(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
    async with httpx.AsyncClient() as client:
        items1 = await fetch_feed("https://example.com/feed.xml", client)
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
    async with httpx.AsyncClient() as client:
        items2 = await fetch_feed("https://example.com/feed.xml", client)
    assert items1[0]["id"] == items2[0]["id"]


_GALLERY_RSS = """\
<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Gallery Feed</title>
    <item>
      <title>Gallery</title>
      <guid>guid-gallery-1</guid>
      <enclosure url="https://example.com/one.jpg" type="image/jpeg" length="0"/>
      <enclosure url="https://example.com/two.gif" type="image/gif" length="0"/>
      <media:content url="https://example.com/three.mp4" type="video/mp4"/>
    </item>
  </channel>
</rss>"""


async def test_fetch_feed_gallery_item_has_media_json(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_GALLERY_RSS))
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    assert len(items) == 1
    item = items[0]
    assert item["media_url"] == "https://example.com/one.jpg"
    assert item["media_type"] == "image"
    assert json.loads(item["media_json"]) == [
        {"url": "https://example.com/one.jpg", "type": "image"},
        {"url": "https://example.com/two.gif", "type": "gif"},
        {"url": "https://example.com/three.mp4", "type": "video"},
    ]


async def test_fetch_feed_single_media_item_has_one_element_media_json(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    img = next(i for i in items if i["media_type"] == "image")
    assert json.loads(img["media_json"]) == [{"url": "https://example.com/photo.jpg", "type": "image"}]


_PUBDATE_RSS = """\
<?xml version="1.0"?>
<rss version="2.0"><channel><title>P</title>
  <item><title>Img</title><guid>g1</guid>
    <pubDate>Sun, 02 Aug 2026 10:00:00 +0000</pubDate>
    <enclosure url="https://example.com/p.jpg" type="image/jpeg" length="0"/>
  </item>
</channel></rss>"""


async def test_fetch_feed_normalizes_pub_date(mock_http: respx.MockRouter) -> None:
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_PUBDATE_RSS))
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    # RFC-822 "Sun, 02 Aug 2026 10:00:00 +0000" must become ISO, not stored verbatim.
    assert items[0]["pub_date"] == "2026-08-02 10:00:00"


async def test_fetch_feed_pub_date_none_when_absent(mock_http: respx.MockRouter) -> None:
    # _RSS (module fixture) has no pubDate → pub_date must be None, not a weekday string.
    mock_http.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=_RSS))
    async with httpx.AsyncClient() as client:
        items = await fetch_feed("https://example.com/feed.xml", client)
    assert items[0]["pub_date"] is None
