"""Helpers shared by every module that logs a client-supplied value.

A single function, in its own module because it now has callers in more than
one package (src.api, src.media) — an underscore-prefixed copy living in one
of those callers was a secretly public contract with no home of its own.
"""


def loggable(value: str | None) -> str | None:
    """A client-supplied string, safe to put in a single-line log record.

    repr escapes the newlines (and carriage returns) that would otherwise
    forge a whole record against the format src/main.py installs, and the
    slice bounds a value nothing else bounds. None passes through unchanged
    (item_id and after_id are both optional query parameters) so an absent
    value still renders as the bare `None` it always has.
    """
    if value is None:
        return None
    return repr(value[:200])
