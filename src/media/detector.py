"""Media type detection for RSS feed entries.

detect_media() probes structured metadata (enclosures, media:content,
media:thumbnail) then falls back to og:image in the entry HTML summary.

detect_all_media() extends detection to produce galleries. Galleries are
built from three tiers:
  1. <enclosure> and <media:content> in RSS order (the primary signal).
  2. <img src=...> tags in <description> HTML (Reddit-style galleries
     where only the first image is emitted as an enclosure).
  3. Single-item fallback: media:thumbnail or og:image when tier 1
     produced nothing.

Media type is determined by file extension only at ingest time. GIF vs image
is distinguished by extension; the proxy can confirm via Content-Type later.
"""

import logging
from html import unescape
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


class _ImgSrcParser(HTMLParser):
    """Collects src attributes of every <img> tag, in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            url = dict(attrs).get("src")
            if url:
                self.srcs.append(url)


def _extract_img_srcs(html: str) -> list[str]:
    """Return <img src=...> URLs from an HTML snippet, in document order, deduped.

    RSS <description> bodies are often entity-escaped (Reddit emits
    &lt;img src=...&gt;). HTMLParser only recognises real '<' tags, so
    unescape first. CDATA-wrapped content has no entities and is untouched.
    Returns [] for empty or missing input.
    """
    if not html:
        return []
    parser = _ImgSrcParser()
    parser.feed(unescape(html))
    seen: set[str] = set()
    out: list[str] = []
    for url in parser.srcs:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


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
                logger.debug(f"detect_media found {media_type} via {key}: {url}")
                return url, media_type

    summary = entry.get("summary", "")
    if summary:
        og_url = _extract_og_image(summary)
        if og_url:
            media_type = detect_type(og_url)
            if media_type:
                logger.debug(f"detect_media found {media_type} via og:image: {og_url}")
                return og_url, media_type

    logger.debug("detect_media found nothing for entry")
    return None


def detect_all_media(entry: dict) -> list[tuple[str, str]]:
    """Return all media of an entry as (url, media_type) pairs, in display order.

    Galleries are built from three tiers:
    1. <enclosure> and <media:content> in RSS order (the primary signal).
    2. <img src=...> tags in <description> HTML — covers Reddit-style
       feeds where only the first image is an enclosure and the rest are
       inline. Tier 2 only fires when tier 1 produced >= 1 slide, so a
       feed with no structured media is never promoted to a gallery on
       the strength of inline thumbnails alone.
    3. Single-item fallback: media:thumbnail or og:image when tier 1
       produced nothing.

    Returns [] when no usable media is found.
    """
    found: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    for key in ("enclosures", "media_content"):
        logger.debug(f"detect_all_media scanning {key}")
        for item in entry.get(key, []):
            url = item.get("url", "")
            media_type = detect_type(url)
            if url and media_type and url not in seen_urls:
                seen_urls.add(url)
                found.append((url, media_type))

    if found:
        img_srcs = _extract_img_srcs(entry.get("summary", ""))
        logger.debug(f"detect_all_media tier1 ({len(found)} slides), tier2 {len(img_srcs)} <img> src(s)")
        for url in img_srcs:
            media_type = detect_type(url)
            if media_type and url not in seen_urls:
                seen_urls.add(url)
                found.append((url, media_type))
        return found

    logger.debug("detect_all_media tier1 empty, falling back to detect_media")
    single = detect_media(entry)
    if single:
        logger.debug("detect_all_media fallback: 1 slide")
    return [single] if single else []
