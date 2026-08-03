"""Canonical identity for a media URL.

The same picture is routinely posted to several feeds, and each feed hands
us a slightly different URL for it — a different query string, a different
host casing, a www. prefix. Item identity is (feed_id, guid), so none of
that is caught by the UNIQUE constraint on items.

media_key() collapses those cosmetic differences into one string that is
stored alongside media_url and used as the cross-feed deduplication key.

Only universally safe rules live here. Host-specific rewrites (mapping a
CDN's preview host back to its origin asset, extracting an upload id) are
deliberately absent: they need to be verified against real feed URLs
before they can be trusted, and a wrong rule collapses two distinct
images into one.
"""

import logging
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


def media_key(url: str) -> str:
    """Return the deduplication key for a media URL.

    Strips the query string and fragment, lowercases the scheme and host,
    and drops a leading ``www.``. Everything else — path casing included —
    is preserved, since path segments are commonly case-sensitive asset IDs.

    Non-URL or unparseable input is returned unchanged so a malformed
    media_url still gets a stable key of its own rather than colliding
    with every other malformed URL.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        logger.debug(f"media_key: unparseable url {url}: {exc}")
        return url

    if not parts.scheme or not parts.netloc:
        logger.debug(f"media_key: no scheme/host in {url}, using it verbatim")
        return url

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    key = urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))
    logger.debug(f"media_key: {url} -> {key}")
    return key
