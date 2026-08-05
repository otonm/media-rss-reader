"""OPML feed list parser.

Reads the OPML file at the configured path and returns a flat list of
{url, title} dicts. Only entries with a non-empty URL are included.
The title falls back to the URL when the OPML entry has no title attribute.
"""

import logging
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


def parse_opml(path: str) -> list[dict[str, str]]:
    """Parse an OPML file and return a list of feed descriptors.

    Returns an empty list if the file exists but contains no feed entries.
    Returns an empty list on malformed XML (consistent with listparser's behavior).
    Raises FileNotFoundError if the path does not exist.
    """
    with open(path, encoding="utf-8") as f:
        try:
            # This parses an operator-provided local config file, not remote request input.
            tree = ElementTree.parse(f)  # noqa: S314
        except ElementTree.ParseError:
            return []

    feeds = []
    for outline in tree.iter("outline"):
        url = outline.get("xmlUrl", "")
        if not url:
            continue
        title = outline.get("title") or outline.get("text") or url
        feeds.append({"url": url, "title": title})

    logger.debug(f"Parsed OPML file {path} with {len(feeds)} feeds")
    return feeds
