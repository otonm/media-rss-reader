# problems.md

Repo-wide over-engineering audit.

Scope: complexity, dead code, and reinvented stdlib/platform features. Correctness,
security and performance were **not** the target — the one blocking correctness bug
below surfaced incidentally and is recorded because nothing else in this file can be
acted on until it is fixed.

Each entry records: where it is, what is actually there, why it earns a cut, what
replaces it, and roughly how many lines go. Confidence is stated where the call is
genuinely arguable rather than clear-cut.


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
| 11 | delete | `sw.js` precaches URLs never requested | 9 | high |
| 12 | yagni | `backfill_seen_media` runs a v14 one-shot forever | 0 | high |
| 13 | stdlib | `src/timing.py` wraps `time.perf_counter` | ~36 | low-med |
| 14 | shrink | Two schema sources + swallowed duplicate-column error | 8 | high |
| 15 | delete | Dead exports, unread `autoscrollBound` | 3 | high |
| 16 | delete | `extend-exclude = ["deduplicators"]` | 2 | high |
| 17 | shrink | `cache_read_meta` folded into `cache_lookup` | 6 | high |
| 18 | delete | `uvicorn[standard]` unused extras | — | medium |

**net remaining: ~-64 lines, -3 transitive packages.**

Suggested order: take #15, #16, #17 (mechanical, no judgement required, ~11 lines),
then #11 (frontend, needs one manual check), then #14, #12 (backend, each touches
tests), and #13, #18 last.
