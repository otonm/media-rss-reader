# Drop Unavailable Posts — Design

Date: 2026-07-29

## Summary

Posts whose every media URL has returned 404 (i.e. the upstream is gone) are
removed from `/api/items` and never re-inserted, even when the entry is still
present in the RSS feed XML. The WebUI stops loading broken posts, and the
seen-tracking queue stops retrying them on every page load.

Detection runs on both the proxy path (user-driven) and the prefetch path
(background). Removal is destructive — a single 404 per URL is enough — and
the tombstone is permanent (no re-check, no un-tombstone). Posts are many;
flakiness is acceptable.

## Decisions (from brainstorming)

- **All-gallery-slides semantics.** A post is dropped only when every URL
  listed in `media_json` (or just `media_url` when `media_json` is NULL) has
  been observed 404. Tracking which URLs are dead needs a small side table.
- **Detection on proxy and prefetch.** Both code paths call the same helper.
  No HEAD probing at fetch time; rely on real fetches that already happen.
- **Tombstone, not column.** Match the existing `seen_guids` pattern: a
  separate `unavailable_guids(feed_id, guid)` table. The item row is deleted;
  the tombstone prevents re-insert on future feed refreshes.
- **No proxy-status change.** The proxy still returns 502 on upstream
  non-success, so the browser's existing `Image.onerror` path is unchanged.

## Backend

### 1. Schema additions (idempotent for fresh DBs; migration for existing)

In `src/db/schema.py`, add to `create_schema`:

```sql
CREATE TABLE IF NOT EXISTS dead_urls (
    url       TEXT PRIMARY KEY,
    marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS unavailable_guids (
    feed_id  TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid     TEXT NOT NULL,
    marked_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feed_id, guid)
);
```

In `src/db/migrations.py`, append two migrations:

- v6: `CREATE TABLE IF NOT EXISTS dead_urls (…)`
- v7: `CREATE TABLE IF NOT EXISTS unavailable_guids (…)`

Schema gets both via `create_schema()` on fresh DBs; existing DBs reach the
same shape via the two migrations.

### 2. New module `src/media/availability.py`

```python
async def mark_url_dead_and_maybe_drop(
    url: str, item_id: str | None, db: aiosqlite.Connection
) -> list[str]:
    """Mark `url` as dead. For every item that contains it, if every URL in
    that item's media list is now dead, DELETE the row and tombstone it.
    Returns the IDs of items dropped by this call."""
```

Flow:

1. `INSERT OR IGNORE INTO dead_urls (url) VALUES (?)`.
2. Candidates: if `item_id` is given, `SELECT id, feed_id, guid, media_url,
   media_json FROM items WHERE id = ?`. Else fall back to scanning all rows
   for `media_url = ?` (rare — both real callers pass `item_id`); the
   `media_json` column is intentionally not text-searched here, so a URL
   that only appears as a non-primary slide in a gallery won't be found
   without `item_id`. Acceptable: callers that observe a non-primary slide
   404 always pass `item_id`.
3. For each candidate, build the URL list:
   - if `media_json` is set: `json.loads(media_json) -> [str]`
   - else: `[media_url]`
4. If the list is non-empty **and** every URL is in `dead_urls`:
   - `DELETE FROM items WHERE id = ?`
   - `INSERT OR IGNORE INTO unavailable_guids (feed_id, guid, marked_at)
     VALUES (?, ?, datetime('now'))` — feed_id and guid come from the
     candidate row captured in step 2.
5. Return the dropped IDs.

Caller responsibility: any cache file for `url` will not exist (we only
cache successes), so no cache eviction is needed on the 404 path.

### 3. Wire the helper into proxy and prefetch

- **`src/api/media.py`** — `proxy_media` gains
  `item_id: str | None = Query(None)`. On upstream non-2xx, call
  `mark_url_dead_and_maybe_drop(url, item_id, db)`. Order: do this **after**
  closing the streaming response (so the failed bytes aren't held). Errors
  from the helper are logged and swallowed; they must not mask the 502 the
  client deserves.

- **`src/media/prefetch.py`** — `_warm(url, client)` becomes
  `_warm(item_id: str, url: str, client: AsyncClient)`. Both callers
  (`warm_startup_cache`, `prefetch_ahead`) already iterate over rows from
  `items` with `id` available, so this is mechanical. On non-2xx, call
  `mark_url_dead_and_maybe_drop(url, item_id, db)` inside the same
  exception-swallowing try.

### 4. Skip tombstoned items on feed refresh

In `src/feeds/sync.py:_refresh_feed`, before the `INSERT OR IGNORE` loop,
load the set once:

```python
async with db.execute("SELECT guid FROM unavailable_guids WHERE feed_id = ?", (feed_id,)) as cur:
    dead_guids = {row["guid"] for row in await cur.fetchall()}
```

Then in the loop, skip items whose `guid` is in `dead_guids` (with a debug
log line). One round-trip per feed refresh is fine.

### 5. Frontend proxy URL change

- **`src/static/cache-queue.js`** — append `&item_id=` to the proxy URL
  inside `downloadOne`.
- **`src/static/feed-view.js`** — same change for the gallery slides
  built in `createMediaWrap` (one URL per slide).

No other frontend behaviour changes — `onItemFailed` still removes the
broken element from the DOM; what changes is that on next page load the
post no longer appears in `/api/items`.

## What stays the same

- `/api/items` and `/api/items/count` queries: no `WHERE` change. Dead rows
  are simply absent from the table.
- Proxy response on upstream non-2xx: still 502.
- `cache-queue.js` worker loop and `Image.onerror` path: unchanged.
- Pruning, eviction, OPML sync, auth, Reddit Feeds proxy.

## Out of scope

- Re-checking / un-tombstoning dead URLs. Dead is dead.
- GC of `dead_urls`. Rows are tiny.
- Frontend visualisation of "this post was dropped" — silent removal is the
  intended UX.
- Authentication / audit trail for removals.

## Tests

`tests/test_availability.py` (new):

- single-media post: 404 → row deleted + `(feed_id, guid)` row in
  `unavailable_guids`
- gallery with 3 slides, 1 slide 404 → row stays, no tombstone
- gallery with 3 slides, all 3 slides 404 → row deleted + tombstone
- 404 on a URL shared by two posts (the URL appears in both items): both
  posts dropped on the final 404, both tombstoned
- prefetch path and proxy path converge to the same DB state (call
  `mark_url_dead_and_maybe_drop` from each and assert identical outcome)
- `mark_url_dead_and_maybe_drop` returns the dropped item IDs

`tests/test_sync.py` (extend): refresh of a feed whose entry was tombstoned
does not re-insert it (404 stays gone).

`tests/test_api.py` (extend): proxy endpoint with mocked upstream that
returns 404 triggers deletion and tombstone (verifies the wiring, not the
helper).

`tests/test_cache.py` is unaffected.
