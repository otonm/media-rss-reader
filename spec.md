# Media RSS Reader — Portable Specification

## 1. What the product is

A personal, continuously-running media reader.

- It is given a set of RSS/Atom feeds.
- It polls them in the background, forever, independent of any UI being open.
- From each feed entry it extracts **only** media (still images, GIFs, videos).
  Entries with no media are discarded. Text, titles and article bodies are never
  rendered.
- It stores extracted media *references* (not the bytes) in a database, and
  caches the *bytes* separately on disk with its own retention policy.
- It presents one full-screen media item at a time in a vertically snapping
  feed, oldest-first, interleaved across feeds.
- It tracks which items the user has seen, durably, and by default never shows a
  seen item again.
- It aggressively pre-fetches ahead of the reader so scrolling never stalls.
- It self-heals: media that is permanently gone, duplicated, or unusable is
  removed from the library and blocked from coming back.

**Non-goals:** article/text reading, multi-user accounts, social features, an
external database server, feed editing inside the app (the feed list is an
operator-supplied file).

### 1.1 The two halves

The system splits cleanly in two, and the split is the most important thing to
preserve when porting:

| Half | Responsibility | Runs |
|---|---|---|
| **Librarian** | Discover feeds, poll them, extract media, deduplicate, prune, warm the byte cache, delete dead media | Continuously, on a timer, with no UI attached |
| **Reader** | Paginate the library, render one item at a time, record seen state, drive prefetch priority | Only while the user is looking |

In the reference implementation these are one process and the Reader talks to the
Librarian over HTTP. On a native platform they can be a background worker plus a
UI, communicating through the database directly. **The HTTP API in §8 is a
contract between the two halves, not an inherent requirement.** A native port
may replace every endpoint with an in-process call, provided the semantics in §8
are preserved — especially pagination (§9) and seen-marking (§8.2).

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Feed** | One RSS/Atom source. Identified by its URL, or by its filename when it comes from a local directory. |
| **Entry** | One `<item>`/`<entry>` in a feed document. |
| **Item** | A stored record derived from an entry that had at least one usable media URL. The unit the reader displays. |
| **Slide** | One media URL belonging to an item. An item has ≥1 slides; >1 makes it a *gallery*. |
| **GUID** | The entry's stable identifier as the feed publisher states it. |
| **media_key** | The canonical identity of a picture across feeds (§5.3). The deduplication key. |
| **Seen** | The user scrolled past the item. Recorded durably and independently of the item row. |
| **Tombstone** | A small record meaning "this entry has already been dealt with — never store it again." |
| **Dead URL** | A media URL the origin has permanently refused. |
| **Cache** | The on-disk store of downloaded media bytes. Distinct from the item database and separately bounded. |

---

## 3. Configuration

Every knob is a scalar supplied by the operator (environment variables in the
reference; equivalently a settings screen or config file). Defaults shown.

### 3.1 Sources

| Key | Default | Meaning |
|---|---|---|
| `FEEDS_DIR` | `/feeds-output` | Directory scanned for `*.xml` feed documents already on local disk. |
| `OPML_PATH` | `/data/feeds.opml` | OPML file listing remote feed URLs. Empty string disables the OPML pass. |
| `DB_PATH` | `/data/db/reader.db` | Item database location. |
| `CACHE_DIR` | `/cache` | Media byte cache location. |

Both sources are used together; their union is the feed list (§6.1).

### 3.2 Schedule

| Key | Default | Meaning |
|---|---|---|
| `OPML_SYNC_INTERVAL` | `3600` s | How often the feed *list* is reconciled. |
| `FEED_REFRESH_INTERVAL` | `900` s | How often every feed's *content* is polled. |

### 3.3 Retention

| Key | Default | Meaning |
|---|---|---|
| `KEEP_ITEMS` | `1000` | Soft cap on stored items. |
| `ITEMS_MAX_AGE_HOURS` | `168` | Seen items older than this are deleted. Unseen items get **4×** this budget. |
| `CACHE_MAX_ITEMS` | `500` | Max cached media files. |
| `CACHE_MAX_AGE_HOURS` | `48` | Max age of a cached file. |
| `CACHE_MAX_BYTES` | `2147483648` | Total cache byte budget. `0` disables. |
| `MEDIA_MAX_BYTES` | `268435456` | Largest single media transfer. `0` disables. |

### 3.4 Behaviour

| Key | Default | Meaning |
|---|---|---|
| `PREFETCH_AHEAD` | `5` | Items warmed ahead of the reader's position. |
| `FEED_INITIAL_COUNT` | `10` | Page size, and the client's forward lookahead. Must be 1..200. |
| `IMAGE_AUTOSCROLL_DELAY_S` | `2` | Dwell per image in autoscroll; also the **minimum** dwell for GIFs and videos. |
| `MEDIA_LOAD_TIMEOUT_S` | `10` | Client-side per-download deadline. Must be 1..300. Exceeding it **deletes the item** (§10.6) — this is a deliberate, destructive policy knob. |
| `ZOOM_TRANSITION_MS` | `200` | Zoom animation duration. `0` snaps. |
| `DEDUP_SIMILARITY` | `97` | Perceptual-hash match threshold, as a percentage of matching bits. `0` disables perceptual dedup (exact-byte and URL dedup always run). |
| `ALLOW_PRIVATE_MEDIA_HOSTS` | `0` | `1` permits media URLs resolving to private/loopback addresses. Off by default (§7.2). |
| `UI_DEBUG` | `0` | `1` shows a diagnostic overlay. |

### 3.5 Server / auth **[stack-specific]**

`PORT`, `LOG_LEVEL`, `AUTH_USERNAME`, `AUTH_PASSWORD`, `AUTH_SECRET_KEY`,
`AUTH_LOCKOUT_ATTEMPTS` (5), `AUTH_LOCKOUT_MINUTES` (15),
`REDDIT_FEEDS_API_URL`.

**Startup validation (fail fast, do not clamp):**

- `AUTH_SECRET_KEY` must be non-empty — an empty signing key makes sessions
  forgeable.
- `AUTH_USERNAME` and `AUTH_PASSWORD` must both be non-empty. Both-empty is
  *not* a safe "no auth" mode: the login flow would accept empty credentials and
  hand the first visitor the enrollment flow, making them the owner.
- `FEED_INITIAL_COUNT` ∈ [1, 200], matching the page-size ceiling the query
  endpoint enforces. A silent clamp is how those two bounds drifted apart once.
- `MEDIA_LOAD_TIMEOUT_S` ∈ [1, 300]. A value of 0 would empty the library on the
  first scroll.

A port that has no auth (single-user native app) drops §3.5 entirely but must
keep the two numeric validations.

---

## 4. Data model

Relational, single-file, embedded. Any embedded store works; the schema below is
written in SQL because the semantics (cascade, uniqueness, NULL ordering) matter.

### 4.1 Tables

```
feeds
  id               TEXT PK        -- sha256(url)
  url              TEXT UNIQUE    -- remote URL, or bare filename for local files
  title            TEXT
  site_link        TEXT           -- channel <link>, informational
  last_fetched_at  TIMESTAMP
  created_at       TIMESTAMP
  etag             TEXT           -- HTTP validator from the last fetch
  last_modified    TEXT           -- HTTP validator from the last fetch
  source_mtime     REAL           -- local-file validator (mtime)

items
  id          TEXT PK             -- sha256(feed_id || guid)
  feed_id     TEXT NOT NULL -> feeds(id) ON DELETE CASCADE
  guid        TEXT NOT NULL
  title       TEXT
  media_url   TEXT NOT NULL       -- first slide's URL
  media_key   TEXT                -- canonical identity of media_url (§5.3)
  media_type  TEXT NOT NULL       -- 'image' | 'gif' | 'video'  (first slide)
  media_json  TEXT                -- JSON array of {url, type}, all slides
  pub_date    TIMESTAMP           -- normalised, sortable; NULL allowed
  fetched_at  TIMESTAMP
  seen_at     TIMESTAMP           -- NULL = unseen
  UNIQUE(feed_id, guid)

seen_media                        -- THE durable seen record
  media_key   TEXT PK NOT NULL
  seen_at     TIMESTAMP NOT NULL
  -- deliberately NO foreign key to feeds

dead_urls
  url         TEXT PK
  marked_at   TIMESTAMP

resolved_guids                    -- "already decided, do not re-examine"
  feed_id     TEXT -> feeds(id) ON DELETE CASCADE
  guid        TEXT
  resolved_at TIMESTAMP
  PK (feed_id, guid)

media_hashes                      -- content identity of downloaded bytes
  url         TEXT PK
  sha256      TEXT NOT NULL
  phash       TEXT                -- NULL when perceptual dedup is off/impossible
  hashed_at   TIMESTAMP

auth_config                       -- [stack-specific]
  key         TEXT PK             -- only 'totp_secret' is used
  value       TEXT NOT NULL

seen_guids                        -- LEGACY, read-only, migration source only
  feed_id, guid PK, seen_at
```

### 4.2 Indexes (all load-bearing)

```
items(feed_id)
items(pub_date DESC)
items(seen_at)
items(fetched_at)
items(media_url)          -- probed inside write transactions; without it,
                          -- a full scan holds the writer lock
items(media_key)          -- probed once per incoming entry
items(feed_id, pub_date, id)   -- matches the ranking window exactly (§9.1)
media_hashes(sha256)
```

### 4.3 The two tombstone tables, and why there are two

They look redundant. They are not; each answers a different question during
ingest, and collapsing them re-introduces a specific bug.

| Table | Written when | Read by | Bug it prevents |
|---|---|---|---|
| `seen_media` | user scrolls past an item | the insert guard | A seen item is pruned, the feed still lists it, the next poll re-inserts it **unseen**. The user sees the same picture forever. |
| `resolved_guids` | an entry was examined and deliberately **not** stored; every prune eviction; and every item dropped because all of its media went dead | the pre-detection skip set | The insert guard keys on `media_key`, which only exists *after* media detection, so a rejected entry leaves no trace in `items` and would be re-parsed and re-detected on every single poll without this. The same gap applies to a pruned or dead-media row: without the tombstone it returns on the next cycle. |

The distinction that survives is `seen_media`'s: it is keyed on `media_key`, not
`(feed_id, guid)`, and has **no foreign key** on purpose. Its predecessor keyed on
`(feed_id, guid)` with a cascade, so removing a feed from the OPML erased the
seen history for every item that feed had ever carried. Keying on `media_key`
also means a cross-posted picture stays seen no matter which feed carried it.

`resolved_guids` *does* cascade: dropping a feed drops its items too, so a
re-added feed should start clean.

### 4.4 Migrations

Schema evolution is an ordered, append-only list of steps plus an integer version
counter stored in the database. On startup: read version *v*, apply steps
`[v..end]` in order, incrementing the counter **after each step** (not once at
the end) so a crash resumes at the failed step.

Rules:
- Never edit or reorder an applied step.
- Every step must be idempotent — schema changes commit outside any transaction
  in most embedded engines, so a crash between "step applied" and "counter
  bumped" replays it.
- A step may be code, not just SQL, when it needs application logic (e.g. the
  backfill that computes `media_key` for existing rows).

---

## 5. Ingest: from feed document to item

### 5.1 Identity

```
feed_id  = sha256_hex(feed_url)             # or sha256_hex(filename) for local files
item_id  = sha256_hex(feed_id || guid)
guid     = entry.id  ?? entry.link  ?? (after detection) first media URL
```

Hash-derived IDs are stable across restarts, need no sequence counter, and let
the uniqueness constraint do all deduplication work.

### 5.2 Media detection

Input: one parsed entry. Output: an ordered list of `(url, media_type)`, possibly
empty.

**Type is decided by file extension only**, on the URL path with the query string
stripped:

```
.gif                                 -> gif
.jpg .jpeg .png .webp .avif          -> image
.mp4 .webm .mov .avi                 -> video
anything else                        -> not media; the URL is skipped
```

`.svg` is deliberately excluded: it is an active document, not a picture.

**Three tiers, in order:**

1. **Structured media.** Walk `enclosures` then `media:content`, in document
   order. Keep every URL with a recognised extension, de-duplicated by exact URL.
2. **Inline images — only if tier 1 produced at least one slide.** Parse the
   entry's summary/description HTML and collect every `<img src>` in document
   order, de-duplicated. Append those with a recognised extension.
   - The description must be **HTML-entity-unescaped before parsing**: many feeds
     emit `&lt;img src=...&gt;` rather than real tags or CDATA.
   - The "only if tier 1 fired" condition matters: it stops a text feed full of
     inline thumbnails and tracking pixels from being promoted to a gallery.
3. **Single fallback — only if tier 1 produced nothing.** Take the first usable
   URL from `media:thumbnail`, else the `og:image` meta tag in the summary HTML.
   Yields at most one slide.

The result list is stored verbatim as `media_json`. `media_url`/`media_type`
always mirror slide 0, for consumers that only handle single media.

### 5.3 Canonical media identity (`media_key`)

```
media_key(url):
    parse url
    if unparseable, or no scheme, or no host:  return url unchanged
    host = lowercase(host); strip a leading "www."
    return  lowercase(scheme) + "://" + host + path      # query and fragment dropped
```

Path case is **preserved** — path segments are commonly case-sensitive asset IDs.
Unparseable input returns verbatim so each malformed URL gets a stable key of its
own rather than all colliding on one.

Deliberately absent: host-specific rewrites (CDN preview host → origin host,
upload-ID extraction). They cannot be trusted without verification against real
feed URLs, and a wrong rule silently collapses two distinct pictures into one.

### 5.4 Publication date normalisation

Feed dates are parsed to a **lexicographically sortable UTC string**
(`YYYY-MM-DD HH:MM:SS`). This is not cosmetic: RSS 2.0 dates are RFC-822, which
begin with a weekday name, so a text comparison sorts them alphabetically. If no
date can be parsed, `pub_date` is NULL — allowed, and handled explicitly by the
ranking (§9.1).

### 5.5 The ingest loop, per feed

```
skip = { guid : guid in items(feed) }
     ∪ { guid : guid in unavailable_guids(feed) }
     ∪ { guid : guid in resolved_guids(feed) }
    -- loaded ONCE per feed, not per entry

document, etag, last_modified = fetch(feed, previous_etag, previous_last_modified)
    -- conditional request; a 304 short-circuits the whole feed:
    -- no download, no parse, no detection, validators written back unchanged

for entry in document.entries:
    guid = entry.id ?? entry.link
    if guid in skip:  continue            # BEFORE detection — this is the point
    slides = detect_media(entry)
    if slides is empty:  continue
    if guid is null:  guid = slides[0].url ; if guid in skip: continue
    row = build_item(feed_id, guid, entry, slides)
    insert_item(row)

update feed.last_fetched_at, feed.etag, feed.last_modified
```

The skip check runs **before** media detection because detection is the expensive
part (HTML parsing per entry, per poll, per feed).

### 5.6 The insert guard

A single statement, shared by the remote-fetch path and the local-file path.
Three conditions must all hold for the row to be stored:

1. No existing row with the same `(feed_id, guid)` — the uniqueness constraint;
   violations are ignored silently.
2. **No existing item in *any* feed with the same `media_key`** — stops one
   picture appearing once per feed that carries it.
3. **No `seen_media` row with the same `media_key`** — stops a pruned-and-seen
   picture being re-inserted from a feed that still lists it.

```
insert_item(row):
    n = INSERT ... WHERE NOT EXISTS(items WHERE media_key = row.media_key)
                     AND NOT EXISTS(seen_media WHERE media_key = row.media_key)
    if n == 0:
        INSERT OR IGNORE INTO resolved_guids(feed_id, guid)   # never re-examine
    return n
```

Both ingest paths **must** share this. When the guard lived only in the remote
path, feeds loaded from the local directory re-surfaced their seen posts on
every sync.

### 5.7 Local-file feeds

A directory is scanned for `*.xml`. Each file becomes a feed row whose `url` is
the **bare filename** (not a path, not a URL), and whose `id` is
`sha256(filename)`.

Change detection uses the file's modification time, stored on the feed row:
unchanged mtime ⇒ no read, no parse, no detection. Storing the validator on the
feed row (rather than beside the file) is what makes it correct: wiping the
database or dropping the feed row also drops the mtime, so the system can never
claim "unchanged" while the items it stands for are gone.

Feed rows whose `url` does not start with `http://` or `https://` are skipped by
the remote-refresh job.

---

## 6. The Librarian: background jobs

Two independent loops, plus one startup task. Each catches and logs its own
errors and retries on the next tick — a failed cycle never stops the loop.

### 6.1 Job A — reconcile the feed list (`OPML_SYNC_INTERVAL`)

```
1. Scan FEEDS_DIR for *.xml; for each changed file, upsert the feed row
   and ingest its entries (§5.5, local path).
2. folder_urls = { filename : *.xml in FEEDS_DIR }
3. opml_urls   = {} 
   if OPML_PATH set and readable:
       for each <outline xmlUrl=...>:
            if basename(xmlUrl) is already a folder filename: skip (folder wins)
            insert-if-absent the feed row
            add to opml_urls
   (A missing OPML file is normal — log and continue.
    A malformed OPML file yields zero feeds — log and continue.)
4. union = folder_urls ∪ opml_urls
5. if union is non-empty:
       DELETE FROM feeds WHERE url NOT IN union     # cascades to items
   else:
       LOG WARNING and delete nothing
```

Step 5's empty-union guard is critical. An empty union nearly always means the
sources are *unreadable* (unmounted volume, companion service restarting), not
that the user removed every feed. Without the guard, a transient mount failure
cascades away the entire library.

This job does **not** fetch feed content. That is Job B's work.

### 6.2 Job B — refresh content (`FEED_REFRESH_INTERVAL`)

```
for feed in feeds where url is http(s):
    try: ingest(feed)              # §5.5
    except: log and continue       # one broken feed must not stop the cycle
prune_items()                      # §6.3 — always runs, even after failures
evict_cache()                      # §7.4 — likewise
```

### 6.3 Pruning

```
# Phase 1 — age
DELETE items WHERE seen_at IS NOT NULL
              AND fetched_at < now - ITEMS_MAX_AGE_HOURS
DELETE items WHERE seen_at IS NULL
              AND pub_date  < now - 4 * ITEMS_MAX_AGE_HOURS

# Phase 2 — count
total = count(items)
if total <= KEEP_ITEMS: stop
excess = total - KEEP_ITEMS
n = min(excess, count(items where seen))
DELETE the n oldest SEEN items      (ORDER BY pub_date ASC, NULLs last)
excess -= n
if excess > 0:
    DELETE the `excess` oldest UNSEEN items   # last resort
```

Unseen items get four times the age budget because the user has not had a chance
to look at them yet.

**Every deletion path writes a `resolved_guids` tombstone for each removed
`(feed_id, guid)`.** This is not optional bookkeeping — see §4.3.

### 6.4 Startup

```
1. Build/validate the database: create schema, run migrations.
2. Start Job A and Job B loops.
3. Immediately run one Job A cycle and one Job B cycle (do not wait for the
   first interval) so a fresh install is populated within seconds.
4. Fire the startup cache warm (§7.5) as a background task — it must not block
   startup or the first UI request.
```

---

## 7. Media bytes: cache, fetch, dedup, death

The item database holds *references*. This subsystem holds *bytes*, on a separate
retention policy, because a media file is orders of magnitude larger than the row
that points at it.

### 7.1 Cache layout

```
{CACHE_DIR}/{sha256_hex(url)}        -- the bytes, no extension
{CACHE_DIR}/{sha256_hex(url)}.meta   -- the upstream Content-Type, plain text
```

Flat directory, hash filename: O(1) lookup, and no filesystem trouble with any
character a URL may contain.

The `.meta` sidecar is **required**, not an optimisation. The data filename is a
bare hash with no extension, so nothing can guess its type; without the sidecar
the cache-hit path falls back to a generic type and no browser will decode a
cached video.

**Write protocol** (order matters):

```
1. write bytes to a temp file with a name UNIQUE PER WRITER
2. set readable permissions (0644) — the cache volume is often shared
3. write the .meta sidecar
4. atomically rename temp -> final
5. on ANY failure or cancellation: unlink the temp file
```

- Unique temp names because **two writers racing on the same URL is the normal
  case**: the reader's own request routinely overlaps the background warm for the
  same item. With a shared temp name, writer B truncates writer A's in-flight
  file, and A's failed rename deletes B's sidecar. With unique names both writers
  fill their own file and both rename onto the same destination — atomic,
  last-one-wins, always correct.
- The sidecar goes **before** the rename so a file visible to a reader always has
  its type.
- Step 5 must catch *cancellation*, not just errors: a user scrolling past
  mid-download cancels the consumer, and that partial file still has to go.

**In-flight registry.** A per-URL claim table (`url -> concurrent downloader
count`). The background warmer *skips* a URL another download already holds; a
user-facing request proceeds regardless, because someone is waiting for those
bytes.

### 7.2 Fetching from origin — the gate

Media URLs come from third-party feed content and the warmer fetches them with no
user session attached. Every fetch is validated:

```
1. Scheme must be http or https. Host must be present.
2. Resolve the host to IP addresses.
3. Unless ALLOW_PRIVATE_MEDIA_HOSTS: reject if ANY resolved address is
   private, loopback, link-local, multicast, reserved, or unspecified.
   (Unwrap IPv4-mapped IPv6 first: ::ffff:127.0.0.1 is loopback in a hat.)
4. Issue the request PINNED to a validated IP, carrying the original hostname
   as the Host header and TLS SNI.
5. Follow redirects MANUALLY, at most 5 hops, re-running steps 1–4 on every
   hop. Resolve relative Location headers against the LOGICAL url (original
   host), never against the pinned-IP url.
```

Step 4 is what closes the DNS-rebinding window: without pinning, the HTTP client
re-resolves the hostname and may reach an address the check never saw.

**Response validation:**

| Condition | Action |
|---|---|
| Status ∈ {403, 404, 410, 451} | **Mark the URL dead** (§7.6), then fail |
| Any other non-success (429, 5xx, timeout, connection error) | Fail **without** touching the database — a busy CDN is not a missing file |
| Content-Type is `image/svg+xml` | Mark dead, fail — active document, and never renderable here |
| Content-Type is not `image/*`, `video/*`, or `application/octet-stream` | Mark dead, fail — an image URL answering with HTML is overwhelmingly a removed post redirected to a landing page |
| Declared `Content-Length` > `MEDIA_MAX_BYTES` | Fail, do not mark dead |
| Running byte total > `MEDIA_MAX_BYTES` mid-stream | Abort. The consumer sees a truncated file; that is the accepted trade for not letting an undeclared stream fill the volume. |

403 is treated as permanent because removed and hotlink-protected media answer
403 far more often than 404 on the sites this reader targets. The cost is
explicit: an origin that 403s every request lacking a `Referer` will have its
items erased rather than merely failing to load.

### 7.3 The tee

The single primitive both consumers share:

```
tee(url, byte_stream, content_type):
    for each chunk:
        write chunk to the cache temp file
        yield chunk onward to the caller
        update a running SHA-256
    on clean completion:
        publish the cache entry (§7.1)
        record the digest for dedup (§7.7)
    on abort/cancel:
        discard the temp file, record nothing
```

Nothing is buffered beyond one chunk. This is what makes a cache miss paint its
first pixel immediately instead of after the whole upstream transfer — the single
largest contributor to the "black screen while loading" symptom in an earlier
design that downloaded-then-replied.

The digest is recorded **only on a complete transfer**: half a file has the wrong
hash.

### 7.4 Cache eviction

Runs after every content-refresh cycle. Three passes, in order, over data files
sorted by modification time:

```
1. delete every file older than CACHE_MAX_AGE_HOURS
2. while surviving count > CACHE_MAX_ITEMS: delete the oldest
3. if CACHE_MAX_BYTES: while total bytes > budget: delete the oldest
```

- Deleting a data file deletes its `.meta` sidecar too (they share an mtime, so
  they age together).
- `.meta` files are **not counted** as entries; `.tmp` files are **skipped
  entirely** — they are in-flight downloads, and unlinking one breaks its writer.
- Pass 3 exists because counting files cannot bound a directory of
  multi-gigabyte videos.

### 7.5 Pre-fetching

Two producers, one shared concurrency cap (**10 in-flight warms**, so a fast
scroll cannot open unbounded outbound connections).

**Startup warm.** Query the first `FEED_INITIAL_COUNT + PREFETCH_AHEAD` items in
the *exact order the reader will request them* — the interleave of §9.1, but with
**unseen rows ordered ahead of seen rows**, because the client defaults to
hiding seen items. Warming in any other order fills the *end* of the library and
leaves page one a guaranteed miss. Warming an already-cached item costs nothing
(the warmer checks disk first), so a restart with a warm cache issues zero
upstream requests.

**Ahead-of-cursor warm.** Given the item the reader is currently on, warm the
next `PREFETCH_AHEAD` items in interleave order, using **the same seen-filter the
client paged with**. The filter has no default of its own — the caller must state
it, so the warm window always matches what is about to be displayed.

A backlog cap (50 outstanding hint-driven warms) drops further hints rather than
queueing them; the startup warm has its own budget and does not count against it.

### 7.6 Dead-URL propagation

```
mark_dead(url, item_id):
    INSERT OR IGNORE dead_urls(url)

    candidates = {}
    if item_id given:
        row = items[item_id]
        if url ∈ slides(row):        # the caller supplies url and item_id
            candidates += row        # independently — verify before trusting
    candidates += all items WHERE media_url = url

    for row in candidates:
        if every slide URL of row is in dead_urls:
            DELETE the item row
            INSERT OR IGNORE unavailable_guids(row.feed_id, row.guid)
    return the dropped item ids
```

Non-primary gallery slide URLs are reachable **only** via `item_id`, since they
are not any row's `media_url`. Callers observing a slide failure must pass it.

### 7.7 Content deduplication

`media_key` (§5.3) catches the same picture behind cosmetically different URLs.
It cannot catch a genuine re-upload: two distinct CDN asset IDs holding identical
or near-identical bytes. This pass does.

Runs on every completed download, when the bytes are already in hand (so it costs
no extra traffic):

```
record(url, sha256_digest):
    phash = perceptual_hash(url) if DEDUP_SIMILARITY > 0 and cached and decodable
    UPSERT media_hashes(url, sha256_digest, phash)

    twins = other urls in media_hashes with the SAME sha256
    if no twins and phash exists:
        twins = other urls whose phash matches within DEDUP_SIMILARITY

    if no twins: return

    candidate = the item whose media_url = url
    if candidate is older (by fetched_at) than every item at the twin urls:
        return                    # this one is canonical; not our problem
    DELETE candidate
    INSERT OR IGNORE unavailable_guids(candidate.feed_id, candidate.guid)
```

Dropping the **newer** duplicate and tombstoning it is what makes the drop stick
across the next poll.

**Perceptual hash** (256-bit block-mean), when enabled:

```
1. decode to greyscale luma (0.299R + 0.587G + 0.114B)
2. centre-crop to 80% of each dimension  (drops watermarks and letterboxing)
3. downscale to 64x64 bilinear, then to 16x16 by BOX (exact 4x4 block mean)
4. average = mean of the 256 cell values
5. bit_i = 1 if cell_i > average else 0     (strict >, ties fall to 0)
```

Match test: `(256 - hamming_distance) * 100 / 256 > DEDUP_SIMILARITY`. At 97 this
drops an image differing by ≤5 bits. Video and undecodable files simply have no
perceptual hash. The comparison is a linear scan over at most `KEEP_ITEMS`
hashes — a BK-tree is the upgrade path if it ever matters.

---

## 8. Reader ↔ Librarian contract

Presented as HTTP because that is the reference transport. A native port may
implement each as a local function call; the semantics below are the contract.

### 8.1 Query the library

```
GET /api/items?unseen=<bool>&after_id=<id>&after_rn=<int>&size=<1..200>
-> 200 [ Item, ... ]        (may be empty; empty means "no more")
-> 410                      cursor anchor no longer exists
```

`Item`:

```json
{
  "id":         "sha256 hex",
  "feed_id":    "sha256 hex",
  "title":      "string or null",
  "media_url":  "first slide URL",
  "media_type": "image | gif | video",
  "media":      [ {"url": "...", "type": "image|gif|video"}, ... ],
  "pub_date":   "YYYY-MM-DD HH:MM:SS or null",
  "fetched_at": "timestamp",
  "seen_at":    "timestamp or null",
  "rn":         12,
  "cached":     true
}
```

- `media` is always present and always non-empty. If stored gallery data is
  missing or unparseable, it falls back to a one-element list built from
  `media_url`/`media_type` rather than failing the request.
- `rn` is the rank the server issued with this row. **The client must echo it
  back** as `after_rn` (§9.2).
- `cached` is a hint, not a promise: the entry can be evicted or warmed moments
  after the response goes out. Only the *primary* slide is checked, so a gallery
  counts as cached once its first slide is on disk — which matches what the
  client's download queue actually prioritises. Checking it must be one batched
  filesystem probe over the page's URLs, not one probe per row.

### 8.2 Mark an item seen

```
POST /api/items/{item_id}/seen?media_url=<url>
-> 200 {"seen_at": "..."}
-> 404 unknown item AND no usable media_url supplied
```

```
atomically:
    row = UPDATE items SET seen_at = NOW WHERE id = item_id RETURNING media_url, seen_at
    if row is null and media_url is not a valid http(s) URL:  404, roll back
    key = media_key( row.media_url  if row exists  else  media_url )
    INSERT OR REPLACE seen_media(key, NOW)
```

The `media_url` parameter is not redundant. Pruning evicts oldest-first while the
reader is served oldest-first, so a refresh cycle routinely deletes the row
*between the page being served and the user scrolling past it*. Without the
parameter the update matches nothing, the request 404s, and `seen_media` — whose
entire job is to outlive pruning — never learns the item was seen.

The stored row always wins when it exists; the parameter is consulted only when
the row is gone, and only if it is a syntactically valid http(s) URL (nothing on
the server side can corroborate it, and a stray value would put a key in
`seen_media` that suppresses unrelated media).

One timestamp value is bound to both writes so they cannot diverge. The whole
thing is one transaction including the 404 path and including cancellation.

### 8.3 Fetch media bytes

```
GET /api/media/proxy?url=<encoded>&item_id=<optional>
-> 200 bytes (correct Content-Type)
-> 404 the URL is not known to the library
-> 502 upstream failed / upstream returned non-media
-> 503 the cached file vanished between lookup and send (Retry-After: 1)
```

```
1. Cache lookup (one stat on the hash-derived name).
   HIT  -> serve the file directly, WITH Range support, using the .meta type.
           No further validation: the name is sha256(url) so it cannot escape
           the cache directory, and a URL can only be in the cache because it
           passed the gate on an earlier request.
2. MISS -> the URL must be known to the library (see below), else 404.
3. Open upstream (§7.2), then stream through the tee (§7.3) to the client
   and the cache in one pass.
```

**Known-URL gate.** One indexed lookup: `url` against a table holding every media
URL of every item — primary and gallery slides alike, one row each, kept in step
with `items` by an `ON DELETE CASCADE` foreign key.

The cache lookup still runs **before** the gate, but only because a hit needs no
gate at all: the cache key is `sha256(url)`, which cannot escape the cache
directory, and a URL can only be in the cache because it passed the gate on an
earlier request.

**Accepted limitation.** The miss path streams and therefore does **not** honour
`Range`. Seeking an uncached video — or a browser's initial byte-range probe —
restarts from zero. The hit path handles Range correctly, so the same video is
seekable on second view. This is the deliberate price of painting the first frame
immediately.

### 8.4 Report unloadable media

```
POST /api/media/failed?url=<encoded>&item_id=<optional>
-> 200 {"dropped": n}
-> 404 unknown URL
```

Marks the URL dead and drops fully-dead items (§7.6). **Gated on the same
known-URL check as the proxy**, because this deletes rows on the client's say-so.

This policy is stricter than the server's own: the server only marks dead on a
*permanent* answer, while this fires on a client-side *timeout*, which cannot
distinguish "gone" from "slow". `MEDIA_LOAD_TIMEOUT_S` is the knob to raise if
usable posts start disappearing.

### 8.5 Prefetch hint

```
POST /api/prefetch/hint   {"item_id": "...", "unseen": bool}
-> 200 {"status": "ok"}
-> 404 item not found
```

Body validation is strict: `item_id` bounded in length (it is a hash everywhere
it is produced; without a bound an enormous value would be read, validated and
bound as a query parameter before the 404), unknown fields **rejected** (a
misspelled `unseen` field silently accepted would warm the wrong window).

The endpoint awaits only the two ranking queries; the warm tasks themselves are
fire-and-forget.

### 8.6 Health and companion status

```
GET /health                       -> {"status": "ok"}   -- no auth, no HTTPS check
GET /api/reddit-feeds/status      -> the companion service's JSON, or 502
```

The companion status proxy: bound the body size (1 MiB), bound the **total**
duration (10 s) separately from any per-read timeout — a service emitting one byte
every nine seconds trips neither a read timeout nor a byte cap — parse the body
once as a validity check and forward the original bytes. Use a **separate
connection pool with a small limit** so an absent or hung optional companion
cannot starve the media proxy. Log transitions, not repeats: the UI polls at 1 Hz
while its modal is open, so only the change into failure warrants a warning.

---

## 9. Pagination: the interleave and the cursor

This is the subtlest part of the system. Get it wrong and the reader silently
skips items or loops forever.

### 9.1 Ranking

```sql
WITH ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY feed_id
                              ORDER BY pub_date ASC, id ASC) AS rn
    FROM items                       -- NOTE: the FULL item set
)
SELECT * FROM ranked
[ WHERE <seen filter> AND <cursor predicate> ]
ORDER BY rn ASC, feed_id ASC, id ASC
LIMIT :size
```

- `rn = 1` is the oldest item of each feed, `rn = 2` the second-oldest, and so
  on. Ordering by `rn` then `feed_id` **interleaves feeds evenly** instead of
  draining one feed before starting the next.
- The window runs over the **full** item set and the seen filter is applied
  **outside** it. That way marking an item seen removes it from the result
  without renumbering anything else.
- The `id` tiebreak appears in the window *and* in every `ORDER BY`. Not
  cosmetic: the anchor is resolved by one statement and the page read by another,
  and two statements can only agree on a rank if ties break deterministically.
  **Change one, change all** — including the prefetcher, which must warm in
  exactly this order.
- NULL `pub_date` is harmless *because* rank comes from the window: undated rows
  sort first and get ranks 1..k, and a row-value comparison involving NULL
  evaluates to NULL, dropping exactly those rows from a cursor page.
- An index on `(feed_id, pub_date, id)` lets the window read in order instead of
  sorting the table. The ranking is materialised twice per page (anchor, then
  page), on the endpoint every scroll hits.

### 9.2 The cursor

Offset pagination is **wrong** here and was tried: with `unseen=true` the result
set shrinks as the client marks items seen, so `page × size` overshoots and
silently skips items.

Keyset pagination on `rn` alone is also wrong: `rn` is recomputed per request and
moves under an outstanding cursor in **both** directions — a prune lowers it, a
row inserted with an older `pub_date` raises it.

The cursor is therefore **(last item's id, the rank issued with it)**:

```
server:
    anchor = resolve after_id in the SAME ranked CTE
    if anchor not found:            -> 410
    bound = min(after_rn, anchor.rn)  (or anchor.rn if after_rn absent)
    page  = rows where (rn, feed_id, id) > (bound, anchor.feed_id, anchor.id)
```

Taking the **lower** bound means a raised rank *reopens* the window instead of
skipping every undelivered row between the two ranks. The reopened rows come back
as duplicates, which the client drops.

410 rather than "rank 0": resolving a vanished anchor to rank 0 would make the
comparison match everything, i.e. page one of the global interleave, which the
client's duplicate filter discards — leaving a cursor that never advances.

### 9.3 The client side of the cursor

```
fetch_page():
    if already fetching or no more: return
    generation = current_generation          # see §9.4
    reanchor = null ; reanchor_attempts = 0 ; back = 0
    loop:
        anchor = reanchor ?? (paginating ? item_at(len - 1 - back) : null)
        if paginating and anchor is null: break        # walked past the oldest held item
        response = GET /api/items?...&after_id=anchor.id&after_rn=anchor.rn

        if generation changed: DISCARD and return

        if response is 410:
            if not paginating: break                   # page one cannot 410
            reanchor = null
            back = (back == 0) ? 1 : back * 2          # exponential walk-back
            continue
        if response not ok: return
        if response is empty: has_more = false ; return

        fresh = response rows whose id we do not already hold
        if fresh is empty:
            if ++reanchor_attempts > 5: break          # non-convergent; stop
            reanchor = { id: last row.id, rn: last row.rn }   # re-anchor on the
            continue                                          # response's own tail
        append fresh ; return
    has_more = false      # cursor exhausted
```

Four separate protections, each for a real failure:

1. **Known-set filter.** A newly appearing feed shifts the interleave and can
   return an item already held. Two copies in the list make id→index lookup
   ambiguous and desync the current position from the DOM.
2. **Re-anchor on the response's tail.** A page can come back as *entirely*
   duplicates. Deriving the next cursor only from appended rows would leave the
   anchor unchanged and repeat the same request forever. Re-anchoring on the
   response's own last row strictly advances, because that row's tuple is the
   page's maximum in the same ordering the predicate compares against. A correct
   server converges in one or two rounds; the attempt cap only stops a
   misbehaving one from hot-looping.
3. **Exponential walk-back on 410.** A feed leaving the feed list cascades its
   whole item set, so the run of dead anchors is not small. Doubling the step
   finds a surviving anchor in log(n) requests. Reloading from page one instead
   would clear the list and throw the user back to the top of the scroll.
4. **`back` advances only on 410.** A re-anchor round is not a walk-back step;
   advancing `back` there would inflate the stride and later step past anchors
   that are still perfectly good.

### 9.4 Reload generation

Any full reload (e.g. toggling the seen filter) increments a generation counter
and clears the item list without waiting for in-flight requests. A response
arriving under an old generation is **discarded**, not merged: it belongs to a
feed the user has already left, and merging would put an item on screen twice.
The in-flight guard is likewise only cleared by the generation that owns it.

### 9.5 Two limitations, stated explicitly

- The anchor's rank is read by one statement and the page by another, so a
  refresh landing between them can still shift it.
- The lower-bound trick only reopens rows *ahead* of the cursor. A row inserted
  *behind* it — typically an undated entry, which the window ranks first in its
  feed — is not delivered until the client reloads from the top.

---

## 10. The Reader: presentation and interaction

Described in platform-neutral terms; the reference is DOM + IntersectionObserver,
but a native pager with a snap listener maps one-to-one.

### 10.1 Layout

- A single vertically scrolling surface, **mandatory snap**, one item per
  viewport, `snap-stop: always` so a fast fling cannot skip an item.
- Each item is either a **placeholder** (spinner, occupying a full viewport) or a
  **loaded media view**. Placeholders are inserted immediately for every known
  item; they are replaced in place when their bytes finish loading. This is what
  makes the scroll length correct from the start.
- Media is letterboxed (`contain`), never cropped.
- Overlays per item: a slide-count badge (hidden when 1) and a seen checkmark.
- The whole surface is dark, chrome-free; controls collapse behind a single
  floating button on narrow screens.

### 10.2 Position tracking

Two independent observers over the item views:

| Observer | Trigger | Purpose |
|---|---|---|
| **Position** | ≥60% of an item visible | Sets the current item; picks the highest visible fraction when several qualify |
| **Seen** | binary enter/leave of the viewport | Fires the seen mark when an item leaves **upward** (its bottom edge crosses above the viewport top) |

The seen observer skips placeholders — it fires again when the placeholder is
replaced by a real media view, with the new element's current visibility.

**The element the position observer reports is authoritative for navigation**,
not any index derived from the item list. The two are separate index spaces (the
list splices out failed items), and sibling-walking from the observed element
cannot be off by one.

On the position change, in order: set the current element; update the current
index; enforce the one-video rule (§10.4); rebuild the download queue; re-arm
autoscroll; top up pagination if within `FEED_INITIAL_COUNT` of the end of the
loaded list.

Pagination top-up is **scroll-driven**: an idle reader issues no requests.

### 10.3 Seen marking

```
post_seen(item):
    if item already marked: return
    mark it locally FIRST (list + on-screen checkmark)
    send a fire-and-forget beacon: POST /api/items/{id}/seen?media_url=...
```

- **Fire-and-forget delivery that survives tab/app close is required.** In-flight
  ordinary requests are cancelled when a page unloads, which lost every mark made
  in the last moments of a session. A queued beacon is delivered regardless.
- Because there is no response to wait for, the local mark must come first; the
  item's own `seen_at` is the double-post guard.
- `media_url` rides along for the reason in §8.2.
- The item on screen when the app closes never *leaves* the viewport, so no leave
  event fires for it. Mark the current item explicitly on page-hide/pause.
- The mark is optimistic: a dropped beacon loses it for that session. A retry
  queue is the upgrade path if drift is ever observed.
- A secondary debounced scroll listener may back up the observer on platforms
  where the observer misses edge cases; both paths funnel through the same
  deduplicating function.

### 10.4 Video rules

- At most **one video plays at a time** — the one the position observer reports.
  Enforce by pausing *every* video in the feed on each transition, not just the
  previously-playing one: newly mounted videos start on their own, and unmuting
  would otherwise leak audio from offscreen items.
- Videos are muted by default; mute is a global toggle applied to every video.
- Videos loop when autoscroll is **off**, and do not loop when it is on.
- Distinguish a **user pause** from a **programmatic pause**: set a flag before
  every programmatic pause and clear it in the pause handler. A pause not
  preceded by that flag is a real user action and suppresses autoplay for that
  item until the user presses play again. Do not infer intent from
  `volumechange` or `seeking` — browsers fire those for their own reasons.
- Never mutate a paused video's muted state during a transition; on some
  platforms that counts as user interaction and permanently suppresses autoplay.

### 10.5 Galleries

An item with more than one slide renders as a horizontally snapping strip inside
its viewport-sized item, with a dot indicator row.

- Slide 0 reuses the element the download queue already fetched. Remaining slides
  load lazily/deferred (`preload: none` for video, lazy loading for images) — a
  20-slide gallery opening 20 connections at once exhausts the per-host
  connection budget and starves pagination behind it.
- Dot indicators track the **fractional** scroll position every frame (each dot
  receives its closeness to the current slide, 1 when centred, 0 a slide away,
  and interpolates size/brightness from it). No transition — the scroll position
  *is* the animation, so it tracks a finger exactly.
- Settled-slide handling is debounced (~60 ms): mark the active slide, pause
  offscreen slide videos, re-point the one-video rule and autoscroll at the new
  slide, and drop any zoom.
- Dot clicks are handled by delegation with the index computed at click time —
  slide removal shifts indices, so a captured index would point at the wrong
  slide.
- A slide whose media errors is **removed** along with its dot. If one slide
  remains, the indicator and arrows are removed. If none remain, the whole item
  fails (§10.6).
- Navigation: `←`/`→` (and on-screen arrows, and horizontal drag) step slides,
  and step to the previous/next **item** at the strip's boundaries.

### 10.6 Download queue

A priority queue over item IDs drained by **3 concurrent workers**.

Priority order on every rebuild (triggered by each position change):

```
1. the current item                       (always first, cached or not)
2. the next `FEED_INITIAL_COUNT` items    -- forward lookahead
3. items behind the cursor not yet loaded -- reversed, nearest first
4. everything else
within bands 2-4: items the server reported as `cached: true` go FIRST
already-loaded and in-flight items are excluded
```

Why 3 workers: one stalled origin must not freeze every placeholder behind it —
including items already on disk that would paint instantly. Three also stays
inside the typical ~6-connections-per-host budget alongside gallery slides.

Why cached-first: a cached item decodes in milliseconds while a miss waits on the
origin. This is what makes scrolling through warm items feel instant. The current
item is exempt — it is what the user is looking at, and must load either way.

Each download carries a **`MEDIA_LOAD_TIMEOUT_S` deadline**. On expiry: cancel the
transfer (clear the source to free the connection), report the URL to
`/api/media/failed`, and fail the item.

**A failed item leaves the feed entirely** — the view *and* the list entry,
together. They must stay in sync: a view with no list entry makes the position
lookup return "not found" and bails before the queue is rebuilt or autoscroll
re-armed. Before removing the node, if it is the current one, scroll to a
neighbour explicitly rather than letting the platform decide where the scroll
lands.

Report the failure to the server **before** splicing the list entry — the media
URL exists nowhere else.

After each rebuild, send a **debounced** (250 ms) prefetch hint for the current
item. Each hint costs the server two ranking passes on the same connection the
pagination query uses; undebounced hints starve the endpoint the scroll depends
on.

### 10.7 Autoscroll

Per-item dwell timers. Not a pixel-scroll animation loop.

| Media type | Advance when |
|---|---|
| image | `IMAGE_AUTOSCROLL_DELAY_S` elapses |
| gif | the GIF's own total duration elapses (measured, see below) |
| video | the video's `ended` event fires |

A **minimum dwell floor** equal to `IMAGE_AUTOSCROLL_DELAY_S` applies to all
three, so a 300 ms GIF or a 1-second video does not read as a skipped item.
Videos longer than the floor advance immediately on end.

"Advance" means: if the item is a gallery and is not on its last slide, step to
the next slide; otherwise snap to the next item.

Rebind on every current-item change **and** every slide change, always unbinding
the previous timer/listener first. Binding for a lookahead item would steal the
`ended` listener from the item actually being watched.

**GIF duration measurement.** Fetch the GIF bytes and scan for Graphic Control
Extension blocks (`0x21 0xF9 0x04`); each carries a 2-byte little-endian delay in
1/100 s. Sum them, clamp to [50 ms, 60 s]. Fall back to the image delay if the
scan yields zero or the fetch fails. Only attempt this for URLs served through
the media proxy (so the bytes are same-origin and likely cached).

### 10.8 Zoom (images only)

Double-tap, double-click, or `z` zooms the current image to exactly 1 image pixel
per screen pixel, and back.

```
scale = image.naturalWidth / image.renderedWidth
if scale <= 1.01: do nothing        # a downscale is not "zoom to 100%"
```

- Implemented as a transform on the image, so nothing reflows and the item's
  existing clipping bounds the scaled picture for free.
- Geometry is **snapshotted at zoom-in and never re-measured while zoomed** — the
  element is transformed, so re-measuring reports the scaled box and each pan
  compounds the error.
- Pan model: with a pointer, the picture *follows the cursor* — cursor at the
  item's left edge shows the picture's left edge, so one sweep across the item
  reaches the whole image. With a finger, the picture drags 1:1. Both clamp so
  the picture's edge never comes inside the item.
- Panning is **never animated**, even when the zoom step is: a transition on the
  transform lags visibly behind the cursor. Apply the transition inline for the
  duration of the in/out step only, then remove it.
- While zoomed, touch scrolling is suppressed (both the vertical feed and the
  horizontal gallery); on mobile the picture must be zoomed out before navigation
  resumes. On desktop the wheel and the navigation keys reset the zoom and move
  on.
- Zoom is reset by: any item change, any slide change, wheel, navigation keys,
  and window resize/rotate (the snapshotted geometry is stale).
- Autoscroll is **suspended** while zoomed and re-armed on zoom-out.
- Reduced-motion preference forces the transition to 0.
- Video is excluded — it keeps its native controls and platform gestures.

### 10.9 Controls and key bindings

| Key | Action |
|---|---|
| `j` / `↓` | next item |
| `k` / `↑` | previous item |
| `←` / `→` | previous / next gallery slide (steps items at the boundary) |
| `a` | toggle autoscroll |
| `m` | toggle mute |
| `s` | toggle show-seen |
| `z` | toggle zoom (anchored at the viewport centre) |
| `Esc` | close the status modal |

Pointer drag (mouse/pen only; touch keeps native scrolling) past a 40 px
threshold is a swipe: horizontal → slide step, vertical → item step. Below the
threshold it is an ordinary click, so on-screen buttons and video controls are
unaffected. Drags starting on a video are ignored entirely — video controls have
their own drag behaviour. Drags while zoomed pan instead.

**Show-seen** is a filter, persisted locally (survives restarts). Toggling it
clears the rendered feed, resets the store (bumping the generation, §9.4), and
refetches from the top.

**Debug overlay** (`UI_DEBUG=1`): a non-interactive fixed panel naming the
current item — feed id, title, media type + file extension, publish date, slide
count, cache HIT/MISS with measured load time, and queue depth. It must not
intercept input.

### 10.10 Client configuration transport

The client's runtime numbers (`FEED_INITIAL_COUNT`, `IMAGE_AUTOSCROLL_DELAY_S`,
`MEDIA_LOAD_TIMEOUT_S`, `ZOOM_TRANSITION_MS`, `UI_DEBUG`) originate on the server.

**[stack-specific]** The reference injects them into the app shell as CSS custom
properties at startup and reads them synchronously before first render, avoiding
a config round-trip. Two constraints if you copy this: the injected block must
come *after* the stylesheet (equal specificity, later wins — with it first, the
stylesheet's defaults silently override every configured value), and static asset
URLs carry a per-startup version token so browser and service-worker caches
cannot serve stale assets across deploys.

A native port simply reads its settings directly.

---

## 11. Authentication **[stack-specific]**

Present because the reference is exposed over a network. A single-device native
app may omit this entire section. If any remote access exists, do not.

### 11.1 Request gate

Every inbound request, in this order:

```
1. /health passes unconditionally (container liveness probes have no proxy headers)
2. reject unless the request arrived over TLS (X-Forwarded-Proto == https) -> 403
3. /login, /setup, /static/* pass without a session
4. otherwise require a valid session cookie, else redirect to /login
```

Order matters: the TLS check runs before the auth-free check, so the login form
is never served over plaintext. The design **assumes a trusted TLS-terminating
reverse proxy** and must not be exposed directly.

### 11.2 Sessions

Stateless signed, timestamped tokens (no server-side session store). 7-day
lifetime. Cookie flags: HttpOnly, Secure, SameSite=Lax. Rotating the signing key
invalidates every active session instantly.

### 11.3 Login and enrollment

Credentials are a username, a password, and a TOTP code. Password comparison must
be constant-time.

```
POST /login:
    if IP is locked out                       -> 429
    if username or password wrong             -> record failure, 401
    if no TOTP secret is enrolled yet:
        generate a fresh base32 secret
        put it in a signed, 10-minute SETUP cookie (never in the database yet)
        -> redirect to /setup
    if TOTP code missing or invalid (±1 step) -> record failure, 401
    reset the lockout counter, issue the session cookie, redirect to /

GET /setup:
    if a secret is already enrolled -> redirect to /login
    verify the setup cookie, else  -> 403
    render the otpauth:// URI as a QR code (rendered client-side, no external
    service) plus the base32 secret as copyable text

POST /setup:
    lockout check; already-enrolled check; setup-cookie check
    verify the code against the PENDING secret
      on failure: record it, re-render with an error, and RE-ISSUE the setup
                  cookie so a retry gets a fresh window instead of expiring
                  mid-enrollment
      on success: persist the secret, clear the setup cookie, issue the session
                  cookie, redirect to /
```

The candidate secret lives only in the signed cookie until the user proves they
can generate a code from it. That is what makes an interrupted enrollment safe.

### 11.4 Lockout

In-process, per client IP, using a **monotonic** clock (immune to system clock
changes). After `AUTH_LOCKOUT_ATTEMPTS` failures the IP is locked for
`AUTH_LOCKOUT_MINUTES`; the counter resets on success, and resets itself once a
lockout window elapses so failures cannot accumulate forever. Login and
enrollment share one tracker. State is lost on restart — acceptable for a
single-process deployment.

---

## 12. Cross-cutting concerns

### 12.1 Concurrency and storage

- **Concurrent readers with a live writer is a hard requirement**: the Librarian
  writes continuously while the Reader queries. Use write-ahead logging or an
  equivalent MVCC mode. Without it every background write blocks every read.
- Enforce foreign keys explicitly if the engine defaults them off — the
  items→feeds cascade is load-bearing.
- Set a generous busy/lock timeout (30 s). The default of a few seconds is too
  tight when many short-lived writers contend.
- **Requests share one process-wide connection**, not one per request. An
  embedded engine typically serialises statements on the connection's own
  worker thread anyway, so every request queues behind every other regardless;
  at that point sharing one connection is cheaper than opening a thread and
  running setup PRAGMAs per request.
- **One shared connection means one shared implicit transaction.** If reads share
  a connection, *every* write on it — even a single statement — must run under an
  application-level write lock that commits on success and rolls back on any
  other exit, **including cancellation**. Two coroutines on one connection
  otherwise commit each other's half-finished work, and an unwound rollback
  leaves the connection holding a write lock until the busy timeout expires.
- Give the background Librarian its **own** connection. It writes many rows per
  cycle before committing; sharing would let an unrelated request commit or roll
  back a partial refresh.
- Writes with no connection to borrow — background warm tasks and streaming
  response bodies, which run *after* the request that started them has returned
  and closed its connection — open their own short-lived connection, and log and
  swallow their failures: they are all fire-and-forget bookkeeping (cache
  digests, dead-URL marks) that must not fail the media they belong to.

### 12.2 Failure philosophy

- A failure in one feed never stops the cycle for the others.
- Pruning and cache eviction run **after** the refresh loop regardless of how
  many feeds failed, so retention limits are enforced unconditionally.
- Background loops catch everything, log, and retry on the next tick.
- Distinguish **permanent** from **transient** upstream failures everywhere. Only
  permanent failures may delete data. This one rule prevents the most damaging
  class of bug in the system: a CDN having a bad ten minutes silently erasing the
  library.
- Shutdown must cancel outstanding background tasks **before** closing the
  resources (HTTP client, connections) they are using.

### 12.3 Logging

- Levels: `error` = fatal, `warning` = recoverable failure, `info` = status
  change an operator should see, `debug` = flow detail.
- **Every string that came from outside the process** — request parameters, and
  equally feed content (GUIDs, titles, URLs) — must be escaped and length-bounded
  before being logged. A feed is a trust boundary exactly like a request path: a
  newline in a title otherwise forges a whole log record.
- Escape once, at the point the value enters the code, because those same values
  end up embedded in exception messages that get re-rendered by outer handlers.
- Attach a per-request correlation id to every log record and echo it in the
  response, so a debug run can be reassembled across modules. Work that outlives
  the request (background warms, streaming bodies) runs after any ambient
  context is torn down, so it must take the id as an explicit parameter.
- Rate-limit repeated identical failures by logging **transitions** rather than
  repeats (§8.6).

### 12.4 Security summary

| Surface | Control |
|---|---|
| Media URLs from feeds | Scheme allow-list, private-address rejection, IP pinning, manual redirect re-validation (§7.2) |
| Media proxy as an open relay | Known-URL gate: only URLs the library already stores can be fetched (§8.3) |
| Client-driven deletion | Same known-URL gate on the failure report (§8.4) |
| Cache path traversal | Filenames are hashes of the URL; no user-controlled path component ever reaches the filesystem |
| SVG / active content | Refused by content type, never cached, never rendered |
| Response sniffing | Send `X-Content-Type-Options: nosniff` on every response |
| Request bodies | Bounded field lengths, unknown fields rejected |
| Log injection | §12.3 |
| Credentials | Constant-time comparison, TOTP second factor, per-IP lockout |

### 12.5 Offline / installability **[stack-specific]**

The reference registers a service worker that pre-caches the app shell and icons
and serves static assets cache-first, network-first for everything else, with API
paths excluded entirely. A native app gets this for free.

---

## Appendix — end-to-end trace

Following one picture from publication to disposal:

```
1.  Publisher adds an entry with an <enclosure> to feed F.
2.  Job A (hourly) confirms F is still in the feed list.
3.  Job B (15-minutely) fetches F with its stored ETag; the ETag changed, so
    the body is parsed.
4.  The entry's GUID is not in items ∪ unavailable_guids ∪ resolved_guids for F,
    so media detection runs: tier 1 finds the enclosure, tier 2 finds two more
    <img> tags in the description. Three slides.
5.  media_key(slide 0) matches no existing item and no seen_media row, so the
    item is stored with all three slides.
6.  The reader is open. The position observer moves; the client requests the
    next page; the interleave places this item at rank 7 of feed F.
7.  The response marks it `cached: false`. The download queue puts it in the
    forward band, behind the cached items.
8.  The client also sends a prefetch hint; the server warms the five items ahead
    of the reader's current position, this one among them.
9.  Whichever gets there first opens the upstream: scheme checked, host resolved,
    address validated as public, request pinned to that IP. 200, Content-Type
    image/jpeg, under the size cap.
10. Bytes stream to the client (or are discarded by the warmer) while being
    written to a unique temp file. On completion: sidecar, then atomic rename.
    The SHA-256 is recorded.
11. That digest matches nothing else, so no duplicate is dropped.
12. The user scrolls past. The seen observer fires; the client marks it locally,
    then beacons the seen POST with the media URL attached.
13. The server sets seen_at and writes seen_media[media_key] in one transaction.
14. The item no longer appears in the default (unseen) feed.
15. Seven days later, pruning deletes the row for age and writes a
    resolved_guids tombstone.
16. Feed F still lists the entry. The next poll's skip set contains its GUID
    (from resolved_guids), so detection never runs for it again — and even if it
    did, the seen_media guard would reject the insert.
17. Forty-eight hours after the download, cache eviction removes the bytes.
    Nothing references them; the picture is fully gone.
```
