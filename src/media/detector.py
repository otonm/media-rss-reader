"""Media type detection for RSS feed entries.

detect_media() probes four locations in a feedparser entry dict, in order
of reliability: enclosures, media:content, media:thumbnail, og:image in
the entry HTML summary. The first match wins.

Media type is determined by file extension only at ingest time. GIF vs image
is distinguished by extension; the proxy can confirm via Content-Type later.
"""

import logging
from html.parser import HTMLParser
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

# Supported extensions per media type. Query strings are stripped before matching.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".svg"}
_GIF_EXTS = {".gif"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}


def detect_type(url: str) -> str | None:
    """Return 'image', 'gif', or 'video' based on the URL file extension.

    Returns None if the extension is not in any of the supported sets,
    which causes the entry to be skipped at ingest time.
    """
    # Strip query string before extracting the suffix so ?v=1 doesn't hide .mp4
    suffix = PurePosixPath(url.split("?")[0]).suffix.lower()
    if suffix in _GIF_EXTS:
        return "gif"
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    return None


class _OGParser(HTMLParser):
    """Minimal HTML parser that extracts the og:image meta content attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.og_image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            attr_dict = dict(attrs)
            if attr_dict.get("property") == "og:image":
                self.og_image = attr_dict.get("content")


def _extract_og_image(html: str) -> str | None:
    """Return the og:image URL from an HTML snippet, or None if absent."""
    parser = _OGParser()
    parser.feed(html)
    return parser.og_image


def detect_media(entry: dict) -> tuple[str, str] | None:
    """Return (media_url, media_type) for the first detectable media in an entry.

    Probe order (first match wins):
    1. entry.enclosures  — standard RSS enclosure
    2. entry.media_content  — media:content namespace
    3. entry.media_thumbnail  — media:thumbnail namespace
    4. og:image in entry.summary HTML

    Returns None if no media is found or no URL has a supported extension.
    """
    for enc in entry.get("enclosures", []):
        url = enc.get("url", "")
        media_type = detect_type(url)
        logger.debug(f"Checking enclosure URL {url} with detected media type {media_type}")
        if url and media_type:
            return url, media_type

    for mc in entry.get("media_content", []):
        url = mc.get("url", "")
        media_type = detect_type(url)
        logger.debug(f"Checking media_content URL {url} with detected media type {media_type}")
        if url and media_type:
            return url, media_type

    for mt in entry.get("media_thumbnail", []):
        url = mt.get("url", "")
        media_type = detect_type(url)
        logger.debug(f"Checking media_thumbnail URL {url} with detected media type {media_type}")
        if url and media_type:
            return url, media_type

    # Last resort: scrape og:image from the entry's HTML summary field.
    summary = entry.get("summary", "")
    if summary:
        og_url = _extract_og_image(summary)
        if og_url:
            media_type = detect_type(og_url)
            logger.debug(f"Checking og:image URL {og_url} with detected media type {media_type}")
            if media_type:
                return og_url, media_type

    return None
