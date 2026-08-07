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

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.logging_utils import loggable

logger = logging.getLogger(__name__)


def item_slides(row: Mapping[str, Any]) -> list[dict[str, str]]:
    """The media slides of an items row: the media_json array, or a 1-element
    fallback built from media_url/media_type.

    This shape — a JSON array of {url, type}, NULL meaning "fall back to the
    primary columns" — was decoded independently in src/api/items.py and
    src/media/availability.py. It lives here because both sides already import
    this module and src/media must not depend on src/api, which is where the
    type that could have been shared lived.

    Rows predating migration v5 have media_json NULL. A truncated or otherwise
    unparseable value falls back the same way rather than taking the caller
    down: the decode used to run unguarded inside a list comprehension over a
    whole page.
    """
    raw = row["media_json"]
    if not raw:
        logger.debug(f"item_slides: item {row['id']} has no media_json (pre-v5 row), using media_url")
        return [{"url": row["media_url"], "type": row["media_type"]}]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(f"item_slides: item {row['id']} has unusable media_json ({exc}), using media_url")
        return [{"url": row["media_url"], "type": row["media_type"]}]


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
        logger.debug(f"media_key: unparseable url {loggable(url)}: {exc}")
        return url

    if not parts.scheme or not parts.netloc:
        logger.debug(f"media_key: no scheme/host in {loggable(url)}, using it verbatim")
        return url

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    key = urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))
    logger.debug(f"media_key: {loggable(url)} -> {loggable(key)}")
    return key
