from html.parser import HTMLParser
from pathlib import PurePosixPath

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".svg"}
_GIF_EXTS = {".gif"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi"}


def detect_type(url: str) -> str | None:
    suffix = PurePosixPath(url.split("?")[0]).suffix.lower()
    if suffix in _GIF_EXTS:
        return "gif"
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    return None


class _OGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og_image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            attr_dict = dict(attrs)
            if attr_dict.get("property") == "og:image":
                self.og_image = attr_dict.get("content")


def _extract_og_image(html: str) -> str | None:
    parser = _OGParser()
    parser.feed(html)
    return parser.og_image


def detect_media(entry: dict) -> tuple[str, str] | None:
    for enc in entry.get("enclosures", []):
        url = enc.get("url", "")
        media_type = detect_type(url)
        if url and media_type:
            return url, media_type

    for mc in entry.get("media_content", []):
        url = mc.get("url", "")
        media_type = detect_type(url)
        if url and media_type:
            return url, media_type

    for mt in entry.get("media_thumbnail", []):
        url = mt.get("url", "")
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
