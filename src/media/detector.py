"""Media type detection for RSS feed entries.

detect_media() probes four locations in a feedparser entry dict, in order
of reliability: enclosures, media:content, media:thumbnail, og:image in
the entry HTML summary. The first match wins.

Media type is determined by file extension only at ingest time. GIF vs image
is distinguished by extension; the proxy can confirm via Content-Type later.
"""

from html.parser import HTMLParser
from pathlib import PurePosixPath

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
    for key in ("enclosures", "media_content", "media_thumbnail"):
        for item in entry.get(key, []):
            url = item.get("url", "")
            media_type = detect_type(url)
            if url and media_type:
                return url, media_type

    summary = entry.get("summary", "")
    if summary:
        og_url = _extract_og_image(summary)
        if og_url:
            media_type = detect_type(og_url)
            if media_type:
                return og_url, media_type

    return None


def detect_all_media(entry: dict) -> list[tuple[str, str]]:
    """Return all media of an entry as (url, media_type) pairs, in display order.

    Galleries are built from enclosures and media:content only (deduped by URL);
    media:thumbnail and og:image stay single-item fallbacks so a thumbnail of
    the main image never becomes a bogus second slide. Returns [] when the
    entry has no usable media.
    """
    found: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for key in ("enclosures", "media_content"):
        for item in entry.get(key, []):
            url = item.get("url", "")
            media_type = detect_type(url)
            if url and media_type and url not in seen_urls:
                seen_urls.add(url)
                found.append((url, media_type))
    if found:
        return found
    single = detect_media(entry)
    return [single] if single else []
