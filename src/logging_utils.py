"""Helpers shared by every module that logs a value from outside this process.

A feed is a trust boundary just like the request path: src.api wraps values a
browser sent (item_id, url); src.media wraps values a feed handed it (guid,
feed_id) when refreshing or deduping — a hostile or compromised feed can forge
a log line exactly as a hostile request can.
"""


def loggable(value: str | None) -> str | None:
    """A client-supplied string, safe to put in a single-line log record.

    repr escapes the newlines (and carriage returns) that would otherwise
    forge a whole record against the format src/main.py installs, and the
    slice bounds a value nothing else bounds. None passes through unchanged
    (item_id and after_id are both optional query parameters), so an absent
    value still renders as `None`.
    """
    if value is None:
        return None
    return repr(value[:200])
