"""OPML feed list parser.

Reads the OPML file at the configured path and returns a flat list of
{url, title} dicts. Only entries with a non-empty URL are included.
The title falls back to the URL when the OPML entry has no title attribute.
"""

import logging

import listparser

logger = logging.getLogger(__name__)


def parse_opml(path: str) -> list[dict[str, str]]:
    """Parse an OPML file and return a list of feed descriptors.

    Returns an empty list if the file exists but contains no feed entries.
    Raises FileNotFoundError if the path does not exist.
    """
    with open(path, encoding="utf-8") as f:
        result = listparser.parse(f.read())
    logger.debug(f"Parsed OPML file {path} with {len(result.feeds)} feeds")

    return [
        {"url": feed.url, "title": feed.title or feed.url}
        for feed in result.feeds
        if feed.url  # skip entries with no URL (e.g. category folders)
    ]
