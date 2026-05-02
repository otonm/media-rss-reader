import logging

import listparser

logger = logging.getLogger(__name__)

def parse_opml(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        result = listparser.parse(f.read())
    logger.debug(f"Parsed OPML file {path} with {len(result.feeds)} feeds")

    return [
        {"url": feed.url, "title": feed.title or feed.url}
        for feed in result.feeds
        if feed.url
    ]
