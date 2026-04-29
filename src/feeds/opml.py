import listparser


def parse_opml(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        result = listparser.parse(f.read())
    return [
        {"url": feed.url, "title": feed.title or feed.url}
        for feed in result.feeds
        if feed.url
    ]
