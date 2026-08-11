# problems.md

Repo-wide over-engineering audit.

Scope: complexity, dead code, and reinvented stdlib/platform features. Correctness,
security and performance were **not** the target — the one blocking correctness bug
below surfaced incidentally and is recorded because nothing else in this file can be
acted on until it is fixed.

Each entry records: where it is, what is actually there, why it earns a cut, what
replaces it, and roughly how many lines go. Confidence is stated where the call is
genuinely arguable rather than clear-cut.

## 3. Gallery arrow handlers duplicate `galleryPrev` / `galleryNext`

**Location:** [src/static/feed-view.js:190-229](src/static/feed-view.js#L190-L229)
vs [feed-view.js:322-341](src/static/feed-view.js#L322-L341)

`buildGallery` attaches click handlers to `prevBtn` and `nextBtn` that reimplement,
line for line, the `galleryPrev()` and `galleryNext()` functions defined 100 lines
below and already exported for the ←/→ keys:

| | button handler | `galleryPrev` / `galleryNext` |
|---|---|---|
| reset zoom | `MRR.zoomController?.reset()` | via `advanceOrNext` → `snapToNext` |
| find wrap | `e.currentTarget.closest(".media-item")` | `currentWrap()` |
| slide index | `Math.round(gallery.scrollLeft / gallery.clientWidth)` | identical |
| step | `gallery.scrollTo({left: (idx±1)*clientWidth, behavior:"smooth"})` | identical |
| at the end | `snapToPrev()` / `snapToNext()` | identical |

Two copies of the same arithmetic means a change to slide stepping has to land twice.

**Replacement:**

```js
prevBtn.addEventListener("click", (e) => { e.stopPropagation(); galleryPrev(); });
nextBtn.addEventListener("click", (e) => { e.stopPropagation(); galleryNext(); });
```

**One behavioural difference to confirm before applying:** the button handlers resolve
the wrap from `e.currentTarget.closest(".media-item")`, while `galleryPrev`/`Next` use
`currentWrap()` — the element the IntersectionObserver last reported. These agree
whenever the button being clicked belongs to the item the feed is snapped to, which is
the only state in which the buttons are reachable (`.gallery-nav` is inside a
viewport-height `.media-item`). Worth one manual check on a two-item feed mid-scroll.

**Cut:** -26 lines.

**Confidence:** High on the duplication; medium on the drop-in equivalence, hence the
check above.

---

## 4. `GET /api/feeds` exists only for a debug overlay that ships disabled

**Locations:** [src/api/feeds.py](src/api/feeds.py) (30 lines),
[src/main.py:102](src/main.py#L102) (router registration),
[src/static/controls.js:158-172](src/static/controls.js#L158-L172) (the sole consumer),
plus tests.

The endpoint returns `SELECT id, title FROM feeds ORDER BY title COLLATE NOCASE`. Its
own docstring names the single consumer:

> "The one consumer is `initDebugOverlay` (src/static/controls.js:158-168), which builds
> a feed-id → title map for the UI_DEBUG overlay."

`initDebugOverlay` returns immediately unless `MRR.config.uiDebug`, which comes from
`--ui-debug`, which comes from `settings.ui_debug`, **default `0`**. So in every default
deployment this is a router module, a route registration, a network round trip that
never fires, and a test file, for a mapping used by one line of debug UI.

The docstring also records that this endpoint has already been trimmed once — it used to
return `item_count` and `unseen_count` from a `LEFT JOIN` + `GROUP BY` over the whole
items table "for counts nobody requested". This is the second pass of the same cut.

**Replacement options, in order of laziness:**

1. Add `feed_title` to the `/api/items` row shape (`_row_to_item` already builds a dict;
   the CTE would need a join on `feeds`). The overlay then needs no second request and
   no map. Costs one join on the hot path — measure before choosing this.
2. Have the overlay display the raw `feed_id` (it already falls back to it:
   `debug.feedTitles[item.feed_id] || item.feed_id`). Delete the endpoint and the fetch
   outright.

Option 2 is the lazy one: the overlay is a diagnostic, and a feed id is a perfectly good
diagnostic identifier.

**Cut:** -30 lines src, plus the router registration, plus its tests.

**Confidence:** High that it is disproportionate; the choice between the two
replacements is a product call.

---

## 5. Three overlapping seen-marking mechanisms

**Location:** [src/static/scroll-controller.js](src/static/scroll-controller.js)

Three independent triggers all funnel into `postSeen(id)`:

1. **`seenObserver`** — `IntersectionObserver` at threshold 0, fires on every binary
   enter/leave of the viewport. Marks on leave-upward.
   ([lines 76-83](src/static/scroll-controller.js#L76-L83))
2. **`onFeedScroll`** — a `scroll` listener debounced at 200ms, calling
   `markItemsAboveViewport()`, which does `querySelectorAll(".placeholder, .media-item")`
   and `getBoundingClientRect()` on **every** matched element.
   ([lines 85-100](src/static/scroll-controller.js#L85-L100))
3. **`pagehide`** → `markCurrent()` — covers the item on screen when the tab closes,
   which by definition never leaves the viewport.
   ([line 38](src/static/scroll-controller.js#L38))

The module's own header comment concedes #2 is redundant:

> "A debounced scroll event listener on #feed acts as a **secondary trigger** (desktop
> browsers fire scroll events on overflow containers reliably). Both mechanisms call
> postSeen() which deduplicates via item.seen_at."

`IntersectionObserver` at threshold 0 is the platform feature for exactly this. It fires
on desktop overflow containers too — the observer's root defaults to the viewport, and
`#feed` scrolling moves items across it.

**#3 is not redundant** and must stay: `pagehide` catches the on-screen item, which no
leave event ever fires for.

**#2 additionally does something #1 deliberately does not.** `onSeen` skips elements
without `dataset.mediaType`:

```js
if (!entry.target.dataset.mediaType) return;   // placeholders are not "seen"
```

`markItemsAboveViewport` has no such guard, so it marks **placeholders** — items whose
media never finished downloading — as seen. That is almost certainly unintended: an item
the user scrolled past while it was still a spinner is recorded as viewed, and
`seen_media` is the durable, survives-pruning record, so it never comes back.

**Replacement.** Delete `onFeedScroll`, `markItemsAboveViewport`, the
`state.scrollTimer` field, and the `state.feed.addEventListener("scroll", ...)`
registration. Keep the observer and `pagehide`.

**Secondary win:** removes a `querySelectorAll` + N × `getBoundingClientRect()` (forced
layout) every 200ms during any scroll — the exact operation that competes with the
scroll it is measuring.

**Cut:** -18 lines, plus the layout thrash.

**Confidence:** High.

---

## 6. `_evict_sync` — three passes, three copies of the same unlink

**Location:** [src/media/cache.py:217-267](src/media/cache.py#L217-L267) (51 lines)

The function evicts by age, then by count, then by bytes. Each pass repeats the same
two-line delete:

```python
f.unlink(missing_ok=True)
f.with_suffix(".meta").unlink(missing_ok=True)
```

three times, with three near-identical `logger.debug(f"Evicting cache file ... due to
...")` lines and three separate counters (`by_age`, `by_count`, `by_bytes`) that exist
only to be joined into one summary log line at the end.

**Replacement:**

```python
def drop(f: Path, why: str) -> None:
    logger.debug(f"Evicting cache file {f} due to {why}")
    f.unlink(missing_ok=True)
    f.with_suffix(".meta").unlink(missing_ok=True)
```

and one counter dict. The three passes stay — they are three genuinely different
budgets — but each shrinks to its loop plus one `drop()` call.

**Cut:** -12 lines.

**Confidence:** High. Pure mechanical simplification, behaviour identical.

---

## 7. 2-second polling loop for pagination top-up

**Location:** [src/static/app.js:204-219](src/static/app.js#L204-L219)

```js
setInterval(() => {
  MRR.controls?.renderDebug();
  const cur = MRR.itemStore.getCurrentIndex();
  const total = MRR.itemStore.getItems().length;
  if (MRR.itemStore.hasMoreItems() && total - cur < MRR.config.feedInitialCount) {
    MRR.itemStore.fetchPage().then(() => { /* append */ });
  }
}, 2000);
```

A timer that wakes twice a minute forever — on a backgrounded tab, on a paused
slideshow, on a feed the user stopped scrolling ten minutes ago — to ask a question that
only changes when the user scrolls.

`scrollController` already runs an `IntersectionObserver` over every item and already
knows, synchronously and exactly, when the current index moves. Infinite scroll is what
`IntersectionObserver` is for.

**Replacement.** Observe the last rendered item as a sentinel (or fire the top-up check
from `onIntersect`, which already computes `idx`). The `renderDebug()` call in the same
timer moves to the same place — `onIntersect` already calls
`MRR.controls?.renderDebug()` on line 68.

**Note:** the debug overlay's queue counters (`stats.loading` / `stats.queued`) currently
refresh on this 2s tick and would stop updating while the feed is still. Given the
overlay is off by default and its purpose is diagnosing a *stalled* queue, that is
arguably a regression worth avoiding — if so, keep a timer but gate it on
`MRR.config.uiDebug`, which reduces it to a debug-only cost.

**Cut:** -14 lines in the default path.

**Confidence:** High on the pagination trigger; the overlay-refresh caveat is real and
noted.

---

## 8. Three `httpx.AsyncClient` instances, and a class used as a namespace

**Locations:** [src/main.py:86-87](src/main.py#L86-L87),
[src/scheduler.py:23-29,72-94](src/scheduler.py#L23-L29)

The app opens three clients:

| client | opened in | used by |
|---|---|---|
| `app.state.http` | `main.lifespan` | media proxy, `prefetch_hint` |
| `app.state.http_status` | `main.lifespan` | reddit-feeds poll (capped at 2 conns) |
| `_state.client` | `start_scheduler` | feed XML fetch, **and `warm_startup_cache`** |

`src/http_client.py`'s module docstring says "**Two clients, not one**" — it is unaware
of the third.

The separation of `http_status` is well-justified and documented (an optional companion
polling at 1 Hz must not starve the media pool). The scheduler's client is not: it
serves feed-XML fetches *and* `warm_startup_cache`, which pulls media — the same traffic
`app.state.http` exists for.

**Consequences of the third client:**

- `stop_scheduler` must close it, which means warm tasks must be cancelled first, which
  is the entire `cancel_prefetch_tasks()` + `_bg_tasks` shutdown dance in
  [prefetch.py:78-89](src/media/prefetch.py#L78-L89) and
  [scheduler.py:87-94](src/scheduler.py#L87-L94).
- `_state.client` is `Optional`, so every use site needs the type to be
  `httpx.AsyncClient | None` and `stop_scheduler` needs an `if _state.client:` guard.

**Replacement.** `start_scheduler(db, client)` takes `app.state.http`. `main.lifespan`
already owns its lifetime and closes it after `stop_scheduler()` returns — which is
already the correct ordering in the existing code.

**Separately, `_State`:**

```python
class _State:
    scheduler: list[asyncio.Task] = []   # noqa: RUF012
    client: httpx.AsyncClient | None = None
    running: bool = False

_state = _State()
```

A class with one instance, three mutable class-level attributes (both needing `noqa:
RUF012` to silence the mutable-class-default rule), used purely as a namespace — beside
a separate module-level `_bg_tasks: set` that does not live in it. Three module globals
do the same job in three lines with no `noqa`.

**Cut:** -12 lines, one HTTP client, two `noqa` suppressions, and most of the shutdown
ordering constraint.

**Confidence:** High on `_State`. Medium-high on the client merge — verify no
feed-fetch-specific timeout or header config is implied before merging.

---

## 9. `_log_outcome` — a module global to demote a log level

**Location:** [src/api/reddit_feeds.py:28-46](src/api/reddit_feeds.py#L28-L46)

```python
_last_reachable: bool | None = None   # None = never polled

def _log_outcome(reachable, message, *, exc_info=False) -> None:
    global _last_reachable
    ...
```

A hand-rolled edge detector: WARNING on the transition into failure, DEBUG while it
persists, INFO on recovery. 20 lines, plus mutable module state, to avoid repeating a
warning for an optional service.

**Costs it imposes:**

- Module-level mutable state that leaks across requests and must be reset between tests
  — [tests/conftest.py:104](tests/conftest.py#L104):
  `monkeypatch.setattr("src.api.reddit_feeds._last_reachable", None)`.
- The comment defending it opens with "Module state is normally a smell" — which is the
  tell.
- The status modal polls at 1 Hz *only while open*. It is not a background poller. The
  log-spam scenario it guards against requires the operator to sit with the modal open,
  watching, while the service is down — i.e. exactly when they want the log lines.

**Replacement.** One WARNING when the fetch fails, or DEBUG unconditionally for the
unreachable case. The operator has a log level; that is the knob.

**Cut:** -20 lines, one module global, one conftest fixture line.

**Confidence:** Medium-high. If you have actually been drowned in these warnings in
production, keep it — but then it belongs behind a shared helper, not in one router.

---

## 10. `cache_stream_write` — dead in production

**Location:** [src/media/cache.py:153-165](src/media/cache.py#L153-L165)

```python
async def cache_stream_write(url, chunks, content_type="application/octet-stream"):
    digest = hashlib.sha256()
    async for chunk in cache_stream_tee(url, chunks, content_type):
        digest.update(chunk)
    return _cache_path(url), digest.hexdigest()
```

Grep across `src/` finds exactly one occurrence: the definition. Every caller is a test:

- `tests/test_dedup.py` — lines 41, 196, 264
- `tests/test_cache.py` — lines 15, 49, 64, 94

The production path is `src/media/fetch.py:tee_to_cache`, which drives
`cache_stream_tee` and accumulates its own `hashlib.sha256()` inline (fetch.py:301,
318). So the digest logic exists twice — once for real, once for tests.

Its docstring — "for callers that don't want the bytes... the content digest is
accumulated for free and used by `src.media.dedup`" — describes an architecture that is
no longer the one in the tree. `src.media.dedup` receives the digest from
`tee_to_cache`, not from here.

**Replacement.** Tests drain `cache_stream_tee` directly, which is a two-line helper in
the test module and tests the code that actually runs:

```python
async def _drain(url, chunks, ct="application/octet-stream"):
    d = hashlib.sha256()
    async for c in cache_mod.cache_stream_tee(url, chunks, ct):
        d.update(c)
    return cache_mod._cache_path(url), d.hexdigest()
```

**Cut:** -13 lines src. Test line count is a wash; test *fidelity* improves — they stop
exercising a function no user reaches.

**Confidence:** High.

---

## 11. Service worker precaches URLs the page never requests

**Location:** [src/static/sw.js:5-24](src/static/sw.js#L5-L24) vs
[src/static/index.html:15,37-44](src/static/index.html#L15)

`sw.js` precaches 13 URLs at install:

```js
cache.addAll([
  "/", "/static/style.css", "/static/app.js", "/static/controls.js",
  "/static/feed-view.js", "/static/item-store.js", "/static/scroll-controller.js",
  "/static/autoscroll-controller.js", "/static/cache-queue.js",
  "/static/zoom-controller.js", "/static/manifest.json",
  "/static/icon-192.png", "/static/icon-512.png", "/static/icon-512-maskable.png",
])
```

`index.html` requests every script and the stylesheet with a cache-busting query:

```html
<link rel="stylesheet" href="/static/style.css?v={{VERSION}}">
<script src="/static/app.js?v={{VERSION}}"></script>
```

where `{{VERSION}}` is `str(int(time.time()))` at process start
([main.py:57](src/main.py#L57)).

The fetch handler matches with:

```js
caches.match(event.request).then((cached) => cached || fetch(event.request))
```

`caches.match` compares the **full URL including the query string** unless
`{ignoreSearch: true}` is passed. `/static/app.js?v=1754851200` never matches the cached
`/static/app.js`. Every script and stylesheet request therefore falls through to
`fetch()`, and the result is never written back to the cache.

**Net effect:** 8 of the 14 precached entries (style.css + 7 JS files) are downloaded at
service-worker install and are never served, ever. The app has no offline capability for
its own code — only `/`, the manifest and the three icons work, and `/` goes through the
network-first branch anyway.

**Two valid fixes, pick one:**

1. **Make the cache work:** `caches.match(event.request, { ignoreSearch: true })`. Now
   the precache serves — but you have re-broken the cache busting `{{VERSION}}` exists
   to provide, since a stale precached `app.js` will be served after a deploy. Needs the
   `install` handler to re-`addAll` on every version change, which means the SW itself
   must carry the version.
2. **Admit it does not offline the app:** cut `addAll` to `["/", "/static/manifest.json",
   "/static/icon-192.png", "/static/icon-512.png", "/static/icon-512-maskable.png"]` —
   the five entries genuinely requested without a query.

Option 2 is the lazy and honest one. Option 1 is only worth it if offline use is an
actual goal — and if it is, the version-in-SW work is required, not optional.

**Cut:** -9 lines, plus 8 pointless downloads per install.

**Confidence:** High on the diagnosis. The choice of fix depends on whether offline
support is a goal — `CLAUDE.md` does not list it under Goals.

---

## 12. `backfill_seen_media` — a one-shot migration that runs forever

**Location:** [src/db/migrations.py:140-166](src/db/migrations.py#L140-L166), called
unconditionally from [src/main.py:75](src/main.py#L75) on every startup.

The function reconciles pre-v14 seen state into `seen_media`. It runs:

1. `SELECT media_url, seen_at FROM items WHERE seen_at IS NOT NULL` — unbounded.
2. A `JOIN` from `seen_guids` onto `items` on `(feed_id, guid)`.
3. `executemany INSERT OR IGNORE` over every row from both, each calling `media_key()`
   in Python.
4. `DELETE FROM items WHERE seen_at IS NULL AND media_key IN (SELECT media_key FROM
   seen_media)`.

`seen_guids` was created by migration v2 and last written by migration v3. **Nothing in
`src/` has written to it since v14 introduced `seen_media`** — confirmed by grep: every
occurrence outside `migrations.py` is a comment or a test fixture.

So step 2 joins against a table that can only ever shrink and never grow, forever, on
every restart. Steps 1, 3 and 4 are idempotent but not free — they scale with `items`
(bounded by `KEEP_ITEMS`, default 1000) and with `seen_media` (unbounded — it is the
durable record that deliberately survives pruning, and nothing prunes it).

The docstring justifies the every-startup run: "Idempotent, so it is safe to run on
every startup; the DELETE doubles as a safety net for anything the insert guard in
sync.py somehow lets through." That conflates two jobs — a v14 backfill and a permanent
consistency sweep — in one unversioned function.

**Replacement.** Split them:

- The backfill is a migration. It cannot be a plain SQL string in `MIGRATIONS` because
  `media_key()` is Python — so give `run_migrations` the ability to hold a callable
  alongside a string, or record a separate `PRAGMA user_version`-style flag in
  `auth_config`. Either way it runs once.
- If the DELETE safety net has earned its keep, it belongs in `prune_items`, which
  already runs on the refresh cycle and already owns "keep `items` consistent".

**Cut:** ~0 lines net, but removes a per-startup full scan of two tables and a dead
`seen_guids` join. `seen_guids` itself then becomes droppable in a future migration.

**Confidence:** High on the analysis, medium on the priority — the cost is a few hundred
milliseconds at boot on a default-sized database.

---

## 13. `src/timing.py` — a module for `time.perf_counter()`

**Locations:** [src/timing.py](src/timing.py) (17 lines),
[tests/test_timing.py](tests/test_timing.py) (28 lines)

```python
def timer() -> Callable[[], float]:
    t0 = time.perf_counter()
    return lambda: (time.perf_counter() - t0) * 1000
```

Three lines of code, an 8-line module docstring, imports in four modules, 9 call sites,
and a dedicated test file.

**The case for keeping it** is stated in its own docstring and is genuine: the
`perf_counter / ×1000 / {:.1f}ms` block was copy-pasted nine times with seven different
variable names and two precisions, and the copies had drifted — one computed duration
after the `async with`, so a 404 exited untimed.

**The case for cutting it:** the drift was in *where the clock was read*, not in the
arithmetic. Reading at the log site is a discipline, not an abstraction — a comment in
`CLAUDE.md`'s logging section would enforce it as well. Against that, you carry a module,
an import in four files, a closure allocation per measurement, and a test for a lambda.

**Honest accounting.** Inlining costs one extra line per call site (9 sites: `t0 =
time.perf_counter()` then `(time.perf_counter() - t0) * 1000` inline in the f-string).
That is roughly +9 lines in `src/api/`, against -17 for the module and -28 for its test.
Net ≈ -36.

**Confidence:** Low-to-medium — this is the one entry where reasonable engineers land on
opposite sides. Listed for completeness, not as a recommendation. If the timing lines
ever become uniform enough to stop drifting, the module has done its job and can go.

---

## 14. Two sources of schema truth, reconciled by a swallowed error

**Locations:** [src/db/schema.py:20-47](src/db/schema.py#L20-L47),
[src/db/migrations.py:126-132](src/db/migrations.py#L126-L132)

`schema.py` claims to hold the *initial* schema:

> "Everything added later (`seen_guids`, `dead_urls`, `unavailable_guids`, the
> `media_json` and `site_link` columns) lives in `migrations.py` and only there."

That is not true. `_CREATE_FEEDS` includes `site_link`, which is also migration v8.
`_CREATE_ITEMS` does *not* include `media_json` (v5) or `media_key` (v10). So the split
between the two files is arbitrary, and the docstring describing it is wrong on its own
first example.

The mismatch is absorbed at runtime by `run_migrations`:

```python
except sqlite3.OperationalError as exc:
    if "duplicate column name" not in str(exc):
        raise
    logger.debug(f"run_migrations step {i} ignored duplicate column error")
```

— a string-matched exception swallow, whose comment explains it exists precisely because
"`create_schema` ... ships the latest schema in CREATE TABLE".

**Why it matters.** Two files can disagree about the schema, and the disagreement is
resolved by an error handler rather than by a rule. Adding a column now requires a
decision — migration only, or both? — with no principle to decide it, and the wrong
choice fails silently.

**Replacement.** Freeze `schema.py` at the genuine v1 shape (drop `site_link` from
`_CREATE_FEEDS`) and let `MIGRATIONS` own everything after. The `try/except
sqlite3.OperationalError` block then has no reason to exist and goes with it — an
`ALTER TABLE` that fails becomes a real, loud failure again.

**Migration safety:** existing databases are unaffected — they already have
`user_version >= 8` and skip v8, and `CREATE TABLE IF NOT EXISTS` is a no-op against
their existing `feeds` table. Fresh databases get `site_link` from v8 instead of from
`CREATE TABLE`. Add a test that a fresh DB and a v1 DB migrated forward have identical
`PRAGMA table_info(feeds)`.

**Cut:** -8 lines, one class of silent failure.

**Confidence:** High.

---

## 15. Dead exports and an unread field

Three small items, verified by grep across `src/static/*.js`:

**a. `feedView.createPlaceholder`** — [feed-view.js:462](src/static/feed-view.js#L462)

Exported on the public object. Occurrences: definition, one internal call from
`appendItem`, the export, and the header comment. No cross-module caller. The header
comment still advertises it as "exposed for app.js's 'append more'" — but `app.js` now
calls `MRR.feedView.appendItem(it)` ([app.js:214](src/static/app.js#L214)), which was
introduced specifically to add the duplicate guard that calling `createPlaceholder`
directly bypasses. Exporting it re-opens the hole `appendItem` closed.

**b. `controls.initDebugOverlay`** — [controls.js:301](src/static/controls.js#L301)

Exported. Occurrences: definition, one call from `init()` (line 298), the export. No
external caller.

**c. `state.autoscrollBound`** — [feed-view.js:38](src/static/feed-view.js#L38)

```js
const state = {
  feed: null,
  currentVisibleEl: null,
  currentEl: null,
  autoscrollBound: false,   // never read, never written
};
```

Single occurrence in the whole tree: this declaration. The name suggests it once
mirrored `autoscrollController`'s binding state, which now lives entirely in that
module.

**Cut:** -3 lines, and one re-opened invariant closed (a).

**Confidence:** High.

---

## 16. Dead ruff config

**Location:** [pyproject.toml:24-25](pyproject.toml#L24-L25)

```toml
# Vendored third-party reference extensions — not ours to lint or format.
extend-exclude = ["deduplicators"]
```

`deduplicators/` does not exist in the working tree and is not tracked
(`git ls-files deduplicators` is empty). It is listed in `.gitignore`, so it was a local
scratch directory that never entered the repo.

One live reference survives in a code comment —
[src/media/dedup.py:94-96](src/media/dedup.py#L94-L96):

> "Index with a BK-tree (see `deduplicators/rededup-master/rededup.js:403`) if this ever
> shows up in a profile."

— a pointer to a path no clone of this repo contains. Either vendor the reference file
or cite it by upstream URL.

**Cut:** -2 lines config, and one unresolvable comment reference fixed.

**Confidence:** High.

---

## 17. `cache_read_meta` has one production caller, three lines away

**Location:** [src/media/cache.py:174-197](src/media/cache.py#L174-L197)

```python
def cache_read_meta(url: str) -> str | None:
    meta = _meta_path(url)
    if not meta.exists():
        return None
    return meta.read_text(encoding="ascii").strip() or None

def cache_lookup(url: str) -> tuple[Path, str] | None:
    path = _cache_path(url)
    try:
        path.stat()
    except FileNotFoundError:
        return None
    return path, cache_read_meta(url) or "application/octet-stream"
```

`cache_read_meta`'s only `src/` caller is `cache_lookup`, immediately below it. (Tests
call it directly in `test_cache.py` and `test_fetch.py` to assert the sidecar's
contents — those can assert via `cache_lookup`'s second return value, which is what the
proxy actually consumes.)

`cache.py` currently exports seven lookup-shaped functions: `_cache_path`, `cache_name`,
`_meta_path`, `cache_read`, `cache_read_meta`, `cache_lookup`, `cache_names_present`.
`cache_read` is genuinely used elsewhere (`prefetch._warm`, `dedup._compute_phash`);
`cache_read_meta` is not.

**Replacement.** Inline the four lines into `cache_lookup` and drop the `.exists()`
check — `read_text` on a missing file raises `FileNotFoundError`, which the surrounding
`try` can catch, saving a stat.

**Cut:** -6 lines, -1 filesystem stat per cache hit.

**Confidence:** High.

---

## 18. `uvicorn[standard]` pulls unused transitive dependencies

**Location:** [pyproject.toml:11](pyproject.toml#L11)

`uvicorn[standard]` installs `uvloop`, `httptools`, `watchfiles`, `websockets`,
`python-dotenv` and `colorama`.

- `websockets` — this app serves no WebSocket route. Grep confirms zero occurrences.
- `watchfiles` — powers `--reload`, a development flag. The Dockerfile does not use it.
- `python-dotenv` — powers `--env-file`. Config comes from `os.environ` via
  `src/config.py:_load_settings`, and `docker-compose.yml` sets the environment
  directly.

`uvloop` and `httptools` do earn their place — they are real throughput wins for an
ASGI app.

**Replacement:** `"uvicorn"`, `"uvloop"`, `"httptools"` as explicit dependencies, and
`watchfiles` in the `dev` extra if `--reload` is used locally (`CLAUDE.md`'s dev workflow
does use `--reload`).

**Cut:** -3 transitive packages from the production image.

**Confidence:** Medium. The saving is image size and supply-chain surface, not runtime.
`[standard]` is a widely-used default and there is a real argument for leaving a
conventional choice alone.

---

## Verified clean — do not re-audit

Two areas were checked and found to have nothing worth cutting. Recorded so the next
pass skips them.

**CSS.** All 36 classes in [src/static/style.css](src/static/style.css) are reachable.
The only two with no literal occurrence in JS or HTML — `pending` and `success` — are
applied dynamically at [controls.js:85](src/static/controls.js#L85):

```js
dot.className = "status-dot " + f.last_status;
```

from the companion API's `last_status` field. 541 lines, no dead rules.

**The item-store cursor recovery.**
[item-store.js:57-140](src/static/item-store.js#L57-L140) — the doubling walk-back on
410, `MAX_REANCHOR_ATTEMPTS`, and the `known`-set duplicate filter. This is ~50 lines of
machinery for what looks like straightforward keyset pagination, and it reads as
over-built. It is not. Each branch defends a specific, reachable failure:

- the doubling step: a feed leaving the OPML cascades its entire item set, so the run of
  dead anchors is not small, and a fixed cap stops pagination permanently once the run
  outgrows it;
- the re-anchor: a page returned entirely as duplicates leaves `after_id`/`after_rn`
  unchanged, so the next request repeats the same one and pagination stalls until reload;
- `MAX_REANCHOR_ATTEMPTS`: bounds a server that does not hold the strictly-advancing
  invariant, so a bad response cannot hot-loop the fetch.

Leave it alone.

---

## Summary

| # | Tag | Item | Lines | Confidence |
|---|-----|------|-------|------------|
| 0 | **bug** | `except TypeError, ValueError:` — app does not import | 1 | certain |
| 1 | delete | Bug archaeology in comments/docstrings | ~500 | high |
| 2 | native | `src/auth/totp.py` wrappers over `pyotp` | 29 | high |
| 3 | shrink | Gallery arrows duplicate `galleryPrev`/`galleryNext` | 26 | high |
| 4 | yagni | `GET /api/feeds` for a default-off debug overlay | 30 | high |
| 5 | delete | Redundant scroll-listener seen-marking | 18 | high |
| 6 | shrink | `_evict_sync` three copies of the unlink | 12 | high |
| 7 | native | 2s `setInterval` pagination poll → IntersectionObserver | 14 | high |
| 8 | yagni | Third `httpx` client + `_State` namespace class | 12 | med-high |
| 9 | yagni | `_log_outcome` transition detector | 20 | med-high |
| 10 | delete | `cache_stream_write` — production-dead | 13 | high |
| 11 | delete | `sw.js` precaches URLs never requested | 9 | high |
| 12 | yagni | `backfill_seen_media` runs a v14 one-shot forever | 0 | high |
| 13 | stdlib | `src/timing.py` wraps `time.perf_counter` | ~36 | low-med |
| 14 | shrink | Two schema sources + swallowed duplicate-column error | 8 | high |
| 15 | delete | Dead exports, unread `autoscrollBound` | 3 | high |
| 16 | delete | `extend-exclude = ["deduplicators"]` | 2 | high |
| 17 | shrink | `cache_read_meta` folded into `cache_lookup` | 6 | high |
| 18 | delete | `uvicorn[standard]` unused extras | — | medium |

**net: -700 lines, -3 transitive packages.**

Suggested order: fix #0, then take #15, #16, #10, #17, #6 (mechanical, no judgement
required, ~36 lines), then #5, #11, #3, #7 (frontend, each needs one manual check),
then #2, #4, #8, #9, #14 (backend, each touches tests), and #1 last and by hand.
