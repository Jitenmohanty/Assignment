# Written Answers

## Section 1 — Broken System (summary)

The root cause of the 30-second timeout on `/api/orders/summary/` was a classic
**N+1 query problem** following a `1 + 3N` pattern. The view fetched only `Order`
rows (`Order.objects.all()`), returning a **lazy queryset**. The serializer then
touched three related accessors per order — `obj.customer.name`,
`obj.items.count()`, and `sum(... for i in obj.items.all())`. Because Django's
**related-object descriptors** load lazily, each accessor fired its own SQL query:
1 (orders) + N (customer) + N (item COUNT) + N (item rows). For 200 orders that
is ~601 round-trips.

The fix (`FixedOrderSummaryView`) pushes the work into the database:
`select_related("customer")` resolves the to-one FK as a SQL JOIN in the single
orders query, and `.annotate(item_count=Count("items", distinct=True),
total_amount=Coalesce(Sum(F("items__quantity") * F("items__unit_price")), 0))`
computes per-order aggregates via **GROUP BY aggregation** in the same query.
Result: ~1 query regardless of order count.

Full incident log: `docs/section1/INVESTIGATION.md`. Query-count evidence
(601 -> 1): `docs/section1/silk-evidence.md`.

## Section 2 — What happens to in-flight tasks if the Celery worker is SIGKILL'd?

**Default behaviour (early-ack) loses the task.** Out of the box, Celery
acknowledges a message to the broker the moment a worker *receives* it
(`task_acks_late=False`). If the process is then `SIGKILL`'d — which, unlike
`SIGTERM`, delivers no signal handler and triggers no warm shutdown or cleanup —
the in-flight task is simply **lost**: the broker already dropped the message on
ack, so nothing redelivers it.

**Our configuration flips this to at-least-once delivery** (`config/settings.py`):

- `CELERY_TASK_ACKS_LATE = True` — the message is acked only *after* the task
  returns. A worker killed mid-run never sends the ack, so the task survives.
- `CELERY_TASK_REJECT_ON_WORKER_LOST = True` — if the worker process dies
  (as opposed to raising a normal exception), the task is **re-queued** rather
  than acked-and-lost.
- `CELERY_WORKER_PREFETCH_MULTIPLIER = 1` — the worker holds at most one
  unacked task, so a kill forfeits a single in-flight job, not a prefetched
  batch that would all need redelivery.

**Redis broker specifics.** Redis has no native per-message ack like AMQP.
Celery emulates it with a **`visibility_timeout`**: an in-progress (unacked)
message becomes visible to other workers again once that timeout elapses — this
is the actual redelivery mechanism after a `SIGKILL`. Caveat: set
`visibility_timeout` **longer than your longest-running task**, otherwise a task
that is still legitimately running gets redelivered and executed a second time
in parallel.

**Consequence: delivery is now at-least-once, so `send_email` must be
idempotent.** A worker killed *after* the provider send but *before* the ack
will run the task again on redelivery. Dedupe at the provider on a stable
`job_id` / idempotency key so a double-run does not send two emails. Summary of
the signal distinction: `SIGTERM` = warm shutdown, currently-running tasks
finish before exit; `SIGKILL` = immediate death, safety relies entirely on
late-ack + reject-on-worker-lost + visibility-timeout redelivery.

## Section 3 — Failure modes of thread-local tenant scoping in async Django views

**Why `threading.local` fails under async.** A `threading.local` binds state to
an **OS thread**. Under sync WSGI this is safe: the server runs one request per
worker thread, so `set_current_tenant` on that thread is private to that request.
Under ASGI/async it breaks. A single event-loop thread **interleaves many
requests** as coroutines: while request A `await`s I/O, the loop resumes request
B **on the same thread**. Both share the one thread-local slot, so B's
`set_current_tenant` overwrites A's value; when A resumes it reads B's tenant.
That is a **cross-tenant data leak** — precisely the catastrophe
`TenantManager.get_queryset` (which injects `.filter(tenant=...)`) exists to
prevent. A second failure mode: `sync_to_async` / `async_to_sync` and thread-pool
executors **hop threads**, so a value set on one thread is missing (or wrong) on
the thread that actually runs the ORM call.

**The fix this codebase already uses: `contextvars.ContextVar`.** In
`tenancy/context.py` the tenant lives in
`_current_tenant = contextvars.ContextVar("current_tenant", default=None)`.
asyncio runs each Task inside its own `contextvars.copy_context()`, so a
ContextVar value follows the **logical request/coroutine**, not the OS thread.
Concurrent coroutines on one event-loop thread therefore each see an **isolated**
value, and `asgiref` propagates the context across the sync/async boundary so
`sync_to_async`-wrapped ORM calls still see the right tenant.

`tenancy/middleware.py` sets it via `token = set_current_tenant(tenant)` and, in
a **`finally` block**, calls `clear_current_tenant(token)` which does
`_current_tenant.reset(token)` — restoring the exact prior state (nesting-safe)
so nothing leaks to the next request on the same worker. Our `tenancy/tests.py`
runs concurrent asyncio tasks bound to different tenants and asserts no leak.

**Remaining caveat:** if you spawn a **bare `threading.Thread`** (not via
`sync_to_async`), the child does not inherit the context — you must capture
`contextvars.copy_context()` and run the target through `ctx.run(...)` yourself.

## Section 4 — Architecture Review

### Question A — Django Admin slow with 500k+ rows (PK index already added)

Adding a PK index does not fix the changelist, because the slowness is not point
lookups. Three root causes and their exact Django mechanisms:

1. **Unbounded `COUNT(*)` for the paginator.** Every changelist render runs a
   full `SELECT COUNT(*)` (and a *second* full count for the "N results"
   header) so it can compute the page range. On 500k rows over a filtered/
   unindexed predicate this is a full scan on each page load. Fix with
   `ModelAdmin.show_full_result_count = False` (drops the unfiltered total
   count), tune `ModelAdmin.list_per_page` / `list_max_show_all`, and for very
   large tables subclass `django.core.paginator.Paginator` to return an
   **estimated** count (e.g. Postgres `reltuples`) via an overridden `count`.

2. **N+1 from related fields in `list_display`.** Any FK column or callable in
   `list_display` that reads `obj.customer.name` fires one query per row — 100
   rows = 100+ queries. Fix with `ModelAdmin.list_select_related`
   (`True` or an explicit tuple) so the changelist JOINs the FKs, and use
   `get_queryset()` with `.prefetch_related()` for M2M. Avoid `list_display`
   callables that issue queries per row.

3. **Expensive filters / search / hierarchy.** `list_filter` on an unindexed
   column forces a full scan to build the filter sidebar; add `db_index=True` or
   a composite `Meta.indexes`. `search_fields` performs `LIKE %term%`, which is
   unsargable — on Postgres use `django.contrib.postgres.indexes.GinIndex` with
   `trigram_ops` (or restrict to prefix `term%`). `date_hierarchy` on a huge
   unindexed timestamp scans per drill-down — index the column or drop it. For
   FK edit widgets, replace the default dropdown (which renders *every* related
   row) with `raw_id_fields` or `autocomplete_fields`.

### Question B — offset vs cursor pagination for a 10k-record endpoint

**Offset pagination** (DRF `rest_framework.pagination.PageNumberPagination` or
`LimitOffsetPagination`, i.e. SQL `LIMIT n OFFSET m`) has two real costs at
scale. First, **performance is O(offset)**: the database must scan and *discard*
every row up to the offset before returning the page, so page 1 is instant but
page 400 reads and throws away ~10k rows on every request — deep pages get
linearly slower. Second, it is **unstable under concurrent writes**: if a row is
inserted or deleted while a user pages through, every subsequent offset shifts,
producing **duplicated or skipped rows** — very visible in mobile infinite
scroll where the user re-fetches "the next 20".

**Cursor pagination** (DRF `rest_framework.pagination.CursorPagination`, i.e.
keyset: `WHERE (created_at, id) < (:last_created, :last_id) ORDER BY created_at
DESC, id DESC LIMIT n`) fixes both. It is **O(log n)** — an index seek to the
cursor position, no discarding — so page depth does not degrade. It is **stable
under mutation** because the cursor references a value, not a position; inserts
elsewhere do not shift the window. Trade-offs: it requires a **stable, unique
ordering** (hence the `(created_at, id)` tiebreak), supports only next/previous
(**no random jump to page 5**), and cannot cheaply show "page 5 of 200".

**When to choose which.** Use **offset** (`PageNumberPagination`) for small,
bounded, admin-style datasets where users expect numbered pages and jumps. Use
**cursor** (`CursorPagination`) for large, frequently-mutating, infinite-scroll
feeds — which describes a 10k-record mobile endpoint, so cursor is the right
default here.

### Question C — File upload security: five distinct attack vectors + Django-layer mitigation

1. **Content-type / extension spoofing.** An attacker uploads `payload.php`
   renamed `photo.jpg`, or sends a false `Content-Type` header. **Never trust
   the browser-supplied `Content-Type` or extension.** Detect the *real* type
   from magic bytes with `python-magic` (`magic.from_buffer(...)`), whitelist
   allowed extensions with `django.core.validators.FileExtensionValidator`, and
   cross-check that the detected MIME matches the claimed extension.

2. **Path traversal in the filename.** A filename like `../../etc/cron.d/x`
   escapes the upload dir. Never write the user's filename directly. Use an
   `upload_to` callable that generates a `uuid4()` name, and rely on Django
   `Storage.get_valid_name()` / `get_available_name()` (which strip path
   separators) rather than the raw `uploaded_file.name`.

3. **Stored XSS via inline HTML/SVG.** An uploaded `.svg` or `.html` served
   inline executes JavaScript in your origin. Serve every upload with
   `Content-Disposition: attachment`, an explicit correct `Content-Type`, and
   `X-Content-Type-Options: nosniff` (via `SecurityMiddleware` /
   `SECURE_CONTENT_TYPE_NOSNIFF = True`); store uploads outside `STATIC`/`MEDIA`
   that is browsed inline, and never render user SVG/HTML inline.

4. **DoS via huge files / zip / decompression bombs.** Set
   `DATA_UPLOAD_MAX_MEMORY_SIZE` and `FILE_UPLOAD_MAX_MEMORY_SIZE`, enforce an
   explicit `size` check in the form/serializer, and — before handing an image
   to Pillow — cap dimensions and set `Image.MAX_IMAGE_PIXELS` to defuse Pillow
   **decompression bombs**. Reject archives you do not need to expand.

5. **Executable upload -> RCE / parser exploits.** A `.php`/`.jsp` (or a
   pickle / crafted image) written into a web-served, executable path can run.
   Store uploads **outside the web root** (ideally object storage) served from a
   path the app server will never execute; **re-encode** images (decode + save)
   to strip embedded payloads and keep **Pillow patched**. Also: if you fetch
   remote upload URLs, block **SSRF** (validate/deny internal IPs), and strip
   **EXIF** metadata to avoid leaking geolocation.
