# problems.md

Repo-wide over-engineering audit.

Scope: complexity, dead code, and reinvented stdlib/platform features. Correctness,
security and performance were **not** the target — the one blocking correctness bug
below surfaced incidentally and is recorded because nothing else in this file can be
acted on until it is fixed.

Each entry records: where it is, what is actually there, why it earns a cut, what
replaces it, and roughly how many lines go. Confidence is stated where the call is
genuinely arguable rather than clear-cut.


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
| 14 | shrink | Two schema sources; swallow kept (crash replay) | 1 | high |
| 15 | delete | Dead exports, unread `autoscrollBound` | 3 | high |
| 16 | delete | `extend-exclude = ["deduplicators"]` | 2 | high |
| 17 | shrink | `cache_read_meta` folded into `cache_lookup` | 6 | high |
| 18 | delete | `uvicorn[standard]` unused extras | — | medium |

**net remaining: ~-57 lines, -3 transitive packages.**

Suggested order: take #15, #16, #17 (mechanical, no judgement required, ~11 lines),
then #11 (frontend, needs one manual check), then #14, #12 (backend, each touches
tests), and #13, #18 last.
