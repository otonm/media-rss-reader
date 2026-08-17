# Simplifications

## 1. Client config as JSON, not CSS custom properties

Five numbers travel through the CSS cascade to reach JS. Verified: **none** of
`--feed-initial-count`, `--image-autoscroll-delay-s`, `--media-load-timeout-s`,
`--zoom-transition-ms`, `--ui-debug` is read by any `var()` in `style.css`. They
are a transport, nothing more — and the fallback copy is already out of sync
(`style.css` defines four of the five, omitting `--media-load-timeout-s`).

**Files:** `src/main.py`, `src/static/index.html`, `src/static/app.js`,
`src/static/style.css`

1. `_build_html` (`main.py:44-65`): build
   `<script>window.MRR_CONFIG = {feedInitialCount: …, imageAutoscrollDelayS: …,
   mediaLoadTimeoutS: …, zoomTransitionMs: …, uiDebug: …};</script>` via
   `json.dumps` instead of the `<style>` block. Keep the `{{VERSION}}`
   substitution untouched.
2. Delete the cascade-order guard (`main.py:61-64`) — it has nothing left to guard.
3. `index.html`: keep the `<!-- CONFIG_VARS -->` marker, delete the four-line
   warning comment at `:17-20`.
4. `app.js:16-32`: `readConfig()` reads `window.MRR_CONFIG` directly. Keep the
   existing per-key fallbacks — they are the defence if injection ever fails.
5. `style.css:20-23`: delete the four config `:root` lines. Leave
   `--spinner-size`, `--control-btn-size`, `--counter-size` and the rest alone.

**Verify:** `tests/test_main.py` asserts on the injected block — this is the one
place a test legitimately changes. Then `UI_DEBUG=1 IMAGE_AUTOSCROLL_DELAY_S=7
uv run uvicorn src.main:app --port 8080`, confirm the overlay appears and image
dwell is 7s.

**Risk:** very low. Same injection point, same synchronous-before-first-render timing.

---

## 2. Callees must not commit inside a caller's transaction

Not a simplification — a correctness bug found while mapping. `api/media.py:212`
wraps `mark_url_dead_and_maybe_drop` in `write_transaction`, but that callee runs
its own `db.commit()` (`availability.py:118` and `:138`). A callee committing
mid-transaction defeats the rollback the wrapper exists to provide. Same shape at
`dedup.py:181`.

**Files:** `src/media/availability.py`, `src/media/dedup.py`

Remove the `db.commit()` calls from both. Every caller either already holds
`write_transaction` or goes through `run_with_own_db` — check both call paths
before deleting, and make the ones that relied on the implicit commit explicit.

**Verify:** `tests/test_availability.py`, `tests/test_dedup.py`,
`tests/test_api.py`. Add one test: a `mark_url_dead_and_maybe_drop` that raises
part-way must leave `dead_urls` unchanged.

**Do this before items 3 and 6** — both touch the same functions, and you want
the transaction boundary correct before restructuring around it.

---

## 3. Merge `unavailable_guids` into `resolved_guids`

Both are `PK(feed_id, guid)`, both cascade to `feeds`, both written only by
`INSERT OR IGNORE`, and both are read by exactly one query in the codebase — the
same UNION at `sync.py:50-56`. Nothing distinguishes them. `spec.md` §4.3's
"why three tombstone tables" only actually separates `seen_media` (different key,
no FK, read by the insert guard).

**Files:** `src/db/migrations.py`, `src/feeds/sync.py`,
`src/media/availability.py`, `src/media/dedup.py`

1. Append migration **v20**, one string, idempotent:
   `INSERT OR IGNORE INTO resolved_guids (feed_id, guid, resolved_at)
   SELECT feed_id, guid, marked_at FROM unavailable_guids` — then a second
   appended step `DROP TABLE IF EXISTS unavailable_guids`. Two steps, not one:
   `db.execute` takes a single statement.
2. Repoint the two writers (`availability.py:127-130`, `dedup.py:107-110`) at
   `resolved_guids`, column `resolved_at`.
3. `_skip_guids` (`sync.py:50-56`): drop the `unavailable_guids` UNION arm, down
   to two.
4. Update the docstrings at `sync.py:38-49`, `availability.py:1-10`,
   `dedup.py:11`.

**Verify:** existing `tests/test_sync.py` and `tests/test_availability.py` cover
the tombstone→skip-set path. Add one migration test (v20 moves rows, is
idempotent on replay) and one end-to-end: an item dropped by
`mark_url_dead_and_maybe_drop` must not be re-inserted by the next poll.

**Risk:** low — semantics identical by construction. Keep a `reason` column only
if you want it in the debug log; nothing reads it.

---

## 4. One `drop_item()` helper

`availability.py:126-131` and `dedup.py:104-114` both do DELETE-item +
INSERT-tombstone. `dedup.py:14-15` admits the mirroring in its own docstring.

**Files:** `src/media/availability.py`, `src/media/dedup.py`

Put `async def drop_item(db, row) -> None` in `availability.py` (it already owns
the tombstone concept); `dedup.py` imports it. Do this **after item 3** so the
helper is written once against the merged table.

**Verify:** both existing test files, unchanged.

---

## 5. One background-task registry

Three coexist: `prefetch._bg_tasks`, `prefetch._hint_tasks`
(`prefetch.py:35-62`), and `scheduler._bg_tasks` (`scheduler.py:19`), with two
tracking helpers and two cancel paths.

**Files:** `src/media/prefetch.py`

Collapse `_bg_tasks` + `_hint_tasks` into one set, delete `_track_hint`, and cap
on `len(_bg_tasks) >= MAX_BACKLOG`. The startup warm is bounded by
`feed_initial_count + prefetch_ahead` (≤205 by config validation) and drains, so
sharing the counter is equivalent in practice.

**Watch:** `prefetch.py:41-47` argues explicitly *against* sharing the counter —
"a draining startup warm would spend the hint path's cap". That is real but
bounded and transient. If you'd rather not accept it, keep two sets and skip this
item; it is the lowest-value entry on the list.

Leave `scheduler._bg_tasks` alone — different lifetime, different owner.

**Verify:** `tests/test_prefetch.py` covers `MAX_BACKLOG` and cancellation. If the
backlog test fails because a startup warm now counts, that is the tradeoff above
surfacing — decide, don't paper over it.

---

## 6. `media_urls` index table — kills the `LIKE` gate

`is_known_media_url` (`availability.py:70-95`) is a two-tier gate whose second
tier is an **unindexed full scan** of `items` with a `LIKE` pattern built through
`json.dumps` to match the column's `ensure_ascii` escaping. That subtlety is
load-bearing and easy to reintroduce as a bug. Its cost is high enough that
`spec.md` §8.3 mandates the gate sit *after* the cache lookup — a
correctness-irrelevant ordering constraint that exists only to hide a slow query.

**Files:** `src/db/migrations.py`, `src/feeds/sync.py`,
`src/media/availability.py`, `src/api/media.py`, `spec.md`

1. Append migration **v21**:
   `CREATE TABLE IF NOT EXISTS media_urls (url TEXT NOT NULL, item_id TEXT NOT
   NULL REFERENCES items(id) ON DELETE CASCADE, PRIMARY KEY (url, item_id))`,
   plus an index on `item_id`, plus a backfill from `media_json`. The backfill
   needs `json_each` — SQLite has it built in, so it stays a SQL step and no
   callable is required.
2. `_insert_item` (`sync.py:59-76`): on `rowcount == 1`, insert one `media_urls`
   row per slide. This is inside the caller's transaction already.
3. Rewrite `is_known_media_url` as `SELECT 1 FROM media_urls WHERE url = ? LIMIT 1`.
   Delete `_escape_like` and the `json.dumps` pattern block.
4. Rewrite `_candidate_items` as a join through `media_urls` — no two-way merge,
   no `_item_urls` re-verification.
5. Rewrite `_all_dead` as a `NOT EXISTS` join against `dead_urls`.
6. `api/media.py:120` and `spec.md` §8.3: the "gate after cache lookup" ordering
   constraint can go. **Leave the code order as it is** — moving it buys nothing
   and the cache lookup is still the cheaper first branch. Just delete the
   comment explaining a constraint that no longer exists.

**Migration correctness:** run both gates side by side and assert agreement for
one release before deleting the old tier, or at minimum add a test that feeds a
non-ASCII gallery slide URL through both. That URL is exactly the case the
`json.dumps` escaping existed for.

**Verify:** `tests/test_availability.py` covers both gate tiers today — those
tests should pass against the new implementation *unchanged*, which is the whole
proof. Plus a migration test that the backfill reproduces every URL
`item_slides` yields.

**Risk:** medium. Largest change on the list, needs a backfill, and it is the
gate protecting against open-relay abuse (`spec.md` §12.4). Do it alone, in its
own commit, after everything in items 1–5 has settled.

**Not in scope here:** making `media_urls` the source of truth and dropping
`media_json` entirely. Bigger blast radius (page query, `_row_to_item`, the API
`media` array, `normalize.item_slides`). Worth it only if you are already
rewriting for a port — note it in `spec.md` and move on.

---

## 7. One shared `ranked_page()`

`db/queries.py` shares the SQL *fragments* but not the *assembly*.
`api/items.py:109-148` builds `conditions`/`params` lists; `prefetch.py:154-171`
inlines its own seen filter and anchor resolution. Sharing fragments only
prevents *ordering* drift — `items.py:104-106` and `spec.md` §8.5 both concede
the warm window "can sit a few rows off" the served page.

**Files:** `src/db/queries.py`, `src/api/items.py`, `src/media/prefetch.py`

Add to `queries.py`:

```
async def ranked_page(db, *, unseen, after=None, size, order=INTERLEAVE_ORDER_BY,
                      columns="…") -> list[Row]
async def resolve_anchor(db, item_id) -> Row | None
```

`after` is the resolved anchor row, so the caller keeps ownership of the
410-vs-`None` decision — `items.py` raises `HTTPException(410)`,
`prefetch.py` returns `None`. Do **not** push that policy into the shared
function; the two callers genuinely differ there.

Both `list_items` and `prefetch_ahead` then call the same two functions.
`warm_startup_cache` calls `ranked_page(order=UNSEEN_FIRST_ORDER_BY, after=None)`.

**Keep:** the `min(after_rn, anchor.rn)` bound stays inside `ranked_page`. It is
load-bearing — pruning deletes lowest-`rn`-first, so every surviving row in that
feed shifts down, and a stale `after_rn` would silently skip exactly the pruned
count.

**Verify:** `tests/test_api.py` (cursor/410/rn) and `tests/test_prefetch.py`
(warm order) unchanged.

---

## 8. One ingest path

`local_xml_sync:98-149` and `_refresh_feed:154-194` are the same sequence —
freshness gate → parse → `_skip_guids` → per-entry → `_insert_item` → update the
`feeds` row — differing only in source and validator. The insert loop is written
twice (`:139-144`, `:184-188`), and that asymmetry is exactly the historic bug
`spec.md` §5.6 warns about.

**Files:** `src/feeds/sync.py`, `src/feeds/fetcher.py`

The remaining asymmetry: `fetch_feed` applies `entry_to_item` internally and
returns items; the local path applies it at the call site. Make both produce
`list[item]`, then share one insert loop:

```
async def _ingest_items(db, feed_id, items) -> int
```

**Do not** try to unify the freshness gates. Three `feeds` columns (`etag`,
`last_modified`, `source_mtime`) for two mechanisms is honest — collapsing them
into one `source_version` trades three columns for string packing and a split on
read. Net zero; explicitly declined.

**Verify:** `tests/test_local_feeds.py`, `tests/test_sync.py`,
`tests/test_detection_skip.py` unchanged. The last one covers both halves of the
re-detection fix and is the real check here.

---

## 9. Drop `seen_guids`

Written by no live code. Exists only so the v19 backfill
(`migrations.py:30-53`) can seed `seen_media` from it: v2 creates, v3 populates,
v19 drains.

**Recommended (safe) version — do this:**

Append **v22**: `DROP TABLE IF EXISTS seen_guids`. Keep v2, v3 and v19 exactly as
they are.

On a fresh database the sequence becomes create → populate (0 rows) → drain
(0 rows) → drop. On an old database the full backfill still runs before the drop.
Correct at every starting version, no operator precondition, no counter risk. The
dead table leaves the live schema, which was the point; the migration *history*
stays, which is what an append-only list is for.

**Aggressive version — only if you want the ~25 lines back:**

Replace steps 2, 3 and 19 with no-op placeholders (`"SELECT 1"`) to preserve list
length, delete `_backfill_seen_media`, and append v22 as above. With no callables
left, `MigrationStep`, the `callable(step)` branch in `run_migrations`, and the
`Awaitable`/`Callable` imports can go too — though `spec.md` §4.4 names
code-migrations as part of the portable model, so keeping that 3-line branch is
defensible.

> **Precondition, and it is not optional.** Every deployed database must already
> be at `PRAGMA user_version >= 19`. A database at v10 still holds un-drained
> `seen_guids` rows; it would run the no-op'd v19, never backfill, and **lose the
> seen history for every pruned item, silently.** Check each DB file before
> shipping this:
> `sqlite3 /data/db/reader.db 'PRAGMA user_version'`

Recommendation: ship the safe version. The aggressive one buys 25 lines for a
silent-data-loss failure mode.

**Verify:** `tests/test_migrations.py` — add a case starting from a
pre-v19 database and assert `seen_media` is populated before the drop.

---

## 10. Frontend: one truth per fact

No known bug today, but the same fact is tracked in several places, which is
where the next one comes from. Four independent commits — do them one at a time.

**Files:** `src/static/{app,controls,item-store,feed-view,autoscroll-controller}.js`

| Fact | Tracked in | Fix |
|---|---|---|
| show-seen | `itemStore.state.showSeen`, `controls.state.showSeen`, `aria-pressed` (**read back as truth**, `controls.js:261`), `localStorage` (read twice: `app.js:34-40`, `controls.js:287`) | `itemStore` owns it; `aria-pressed` rendered *from* state, never read; one `localStorage` read at startup |
| mute | `controls.state.muted`, `MRR.config.mutedDefault`, each `<video>.muted` (re-swept in 3 places) | one owner + one `applyMute()` sweep |
| autoscroll | `autoscrollController.state.autoscroll`, `MRR.config.autoscroll`, `aria-pressed` | `MRR.config.autoscroll` exists **only** so `feed-view.js:87` can set `video.loop` — which `autoscroll-controller.js:32` already sweeps. Delete the `feed-view` line and the config field. |
| cached-ness | `cacheQueue.state.cached` Set + `item.cached` written back at `cache-queue.js:92` | the write-back serves only the debug overlay; have `renderDebug` read the Set |

**Rule for all four:** `aria-pressed` is a rendering of state, never a source of
it. Two of the four bugs-in-waiting here are exactly that inversion.

**Verify:** `tests/static/seen-toggle.test.mjs`,
`tests/static/cache-queue.test.mjs`, `tests/static/debug-overlay.test.mjs`.

---

## 11. Frontend: one media-element factory

`/api/media/proxy?url=…&item_id=…` is written literally in **two** places
(`cache-queue.js:139`, `feed-view.js:153`) and pattern-matched in a third
(`autoscroll-controller.js:114`). Element construction and video attributes are
spread across `downloadOne`, `buildGallery` and `wireVideo` — three overlapping
attribute sets on the same elements.

**Files:** `src/static/{cache-queue,feed-view,autoscroll-controller}.js`

1. `proxyUrl(url, itemId)` in one place; all three sites use it, including
   `autoscroll-controller`'s prefix test.
2. `mediaEl(item, slide)` returning a configured `<img>`/`<video>`; `downloadOne`
   and `buildGallery` both call it, `wireVideo` keeps only the play/pause flags.
3. With construction unified, fold the **three error-removal paths**
   (`downloadOne` timeout → `item-failed`; the per-element `error` listener at
   `feed-view.js:123`; `removeSlide`) toward one. `removeSlide` stays distinct —
   it drops a slide, not an item — but it should call the shared failure path
   when the last slide goes.

**Verify:** `tests/static/feed-view.test.mjs` (869 lines) and
`cache-queue.test.mjs`.

---

## 12. Frontend: one "which element is current?" resolver

`feedView.currentWrap()` (`feed-view.js:287-295`) prefers the observed element
and falls back to the store. `autoscrollController.currentVisibleWrap()`
(`autoscroll-controller.js:41-44`) does the store-index lookup only. `spec.md`
§10.2 is explicit that **the observed element is authoritative** — so the second
one is wrong-by-spec on the splice path, where the two index spaces diverge.

**Files:** `src/static/{feed-view,autoscroll-controller}.js`

1. Delete `currentVisibleWrap`; call `feedView.currentWrap()`.
2. Add `wrapById(id)` and use it for the O(n) `isRendered` scan
   (`feed-view.js:43-49`) and the five scattered
   `querySelector('[data-id="…"]')` sites.

**Verify:** `tests/static/autoscroll-controller.test.mjs`,
`feed-view.test.mjs`. Add one: after `onItemFailed` splices the current item,
autoscroll must bind to the element the observer reports, not the store index.

---

## 13. Prose → `spec.md` pointers

Several backend modules are ~50% rationale prose. `list_items` carries a
**50-line docstring** (`items.py:57-107`); `fetch.py:42-77` and
`connection.py:27-128` are similar. That rationale is now duplicated in `spec.md`
§9 and §12.1 — which is where a porter reads it. Two copies that can disagree.

**Files:** `src/api/items.py`, `src/media/fetch.py`, `src/db/connection.py`,
`src/media/prefetch.py`

Replace each long docstring with a two-line summary plus `see spec.md §9.2`.
Comments *inside* functions, explaining a specific line, stay untouched — those
are the ones that stop someone deleting a load-bearing line.

Do this **last**. It touches many files and would conflict with every item above.

**Verify:** `uv run ruff check .` and `uv run pytest`. Nothing behavioural.

---

## Declined

- **Frontend: zoom reset, 10 call sites → 2.** The eight "extra" reset call sites
  (app.js, feed-view.js dots/arrows) are synchronous and fire *before* their
  scroll starts, resetting zoom eagerly so the picture is never seen sliding
  while zoomed. The two "choke points" (setCurrentEl, onGalleryScroll) are
  asynchronous and 60 ms late, and serve as fallback for paths with no input
  event (swipes, autoscroll, onItemFailed). Deleting the eight would leave a
  zoomed image visibly sliding for a frame or 60 ms before snapping back —
  an observable regression, not a simplification. The two sets are a deliberate
  ordering pair; the comments have been corrected to reflect this.
- **Gallery dots via CSS `animation-timeline: scroll(x)`.** `paintDots`
  (`feed-view.js:213-218`) writes a `--t` property per dot on every scroll event,
  undebounced, and native scroll-driven animations express exactly this with no
  JS. Genuinely appealing — but it is gated on a browser-support decision only
  you can make. Check your targets; if they cover it, promote this to the list.
- **Splitting `controls.js`.** It holds three unrelated concerns (button wiring,
  the Reddit-Feeds modal at `:19-138`, the debug overlay at `:152-214`).
  Splitting moves lines rather than deleting them. Skip unless it blocks something.
- **Collapsing the feed freshness validators** (item 8) — net zero.
- **Making `media_urls` the source of truth over `media_json`** (item 6) —
  port-time change, not a maintenance change.

## Not to be touched

Each looks redundant and is not:

- **The cursor's server-side anchor lookup and the `min(after_rn, anchor.rn)`
  bound.** Checked: the client already knows `id` and `feed_id`, so the lookup's
  only product is `anchor.rn` — but pruning deletes lowest-`rn`-first, so a stale
  `after_rn` is too high and would silently skip exactly the pruned count.
- **The 410 path, exponential walk-back and re-anchor loop**
  (`item-store.js:59-137`). Four interacting guards, each traceable to a stated
  failure, and the most heavily tested code in the project.
- **The SSRF gate** (`fetch.py:79-218`) — IP pinning, manual redirect
  re-validation, IPv4-mapped unwrapping.
- **`seen_media`'s missing FK and `media_key` keying** — `spec.md` §4.3.
- **The tee's unique temp names** (§7.1) — two writers racing on one URL is the
  normal case.
- **The three DB write disciplines** (`write_transaction` / scheduler connection
  / `run_with_own_db`) — §12.1; genuinely different lifetimes. Item 2 fixes the
  one real defect in that area.
