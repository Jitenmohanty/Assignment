# Incident Investigation Log — `/api/orders/summary/` timeout regression

**Endpoint:** `GET /api/orders/summary/?customer_id=<id>`
**Symptom:** ~80ms under normal load; after a routine deployment, 30s+ timeouts for
users with **more than 200 orders**. No code change was made to that view.
**Verdict (up front):** **N+1 query regression** — the request count now grows
linearly with the number of orders, following a `1 + 3N` pattern.

---

## 1. Investigation narrative (what I checked, in order, and why)

### First — I read the symptom, not the code
The single most diagnostic fact is that the latency **scales with the number of
orders**: fine for small accounts, catastrophic past ~200 orders. A fixed-cost
problem (a bad config, a slow single query, a missing index on a lookup column)
would slow *everyone* roughly equally. Latency that is a function of row count
means the endpoint is doing per-row work — the classic signature of an N+1. So
before touching anything, my hypothesis was already "work proportional to N,"
and every subsequent step was aimed at confirming or killing that.

### Second — rule out infrastructure and the database engine
On-call discipline: eliminate the cheap, common causes before the subtle ones.
- **App/DB CPU, memory, connection pool:** checked host metrics. If a slow query
  were pinning the DB, CPU/IO would spike globally and *all* endpoints would
  degrade. They didn't — only this endpoint, and only for large accounts. Rules
  out a saturated box or exhausted pool.
- **Missing index:** checked `EXPLAIN` on the orders query. The model already has
  `Index(fields=["customer", "-created_at"])` (see `orders/models.py`), so
  "this customer's orders, newest first" is an index range scan, not a
  filter-then-sort. A missing index would make *one* query slow; it would not
  produce hundreds of queries. Ruled out.

### Third — read the APM trace / slow-query log
This is where the cause becomes visible. The trace for a 200-order request shows
**~601 SQL statements**, not one slow one. Every individual statement is fast
(sub-millisecond); the 30s is the sum of hundreds of round-trips. The log is
dominated by three near-identical statements repeated once per order:
`SELECT ... FROM customer WHERE id = ?`, `SELECT COUNT(*) FROM orderitem WHERE order_id = ?`,
and `SELECT ... FROM orderitem WHERE order_id = ?`. That is `1 + 3N`, which for
N=200 is exactly the observed 601. Confirmed: **N+1.**

### Why "no code change to the view" is consistent with this
The view (`BrokenOrderSummaryView` in `orders/views.py`) genuinely didn't change
— it still does `Order.objects.all().filter(customer_id=...)`. The regression
lives in the **serializer**. During the deployment, `BrokenOrderSummarySerializer`
gained (or reverted to) `SerializerMethodField`s that reach across relations per
object: `get_customer_name` reads `obj.customer.name`, `get_item_count` calls
`obj.items.count()`, and `get_total_amount` iterates `obj.items.all()`. The view
supplies a plain queryset with no `select_related`/`prefetch_related`, so those
accessors have nothing cached to read from. Equivalent real-world triggers:
a new `SerializerMethodField` added, a `related_name` change, or a shared
serializer mixin that used to `prefetch_related` losing that call in the merge.
The view file is innocent; the per-row cost was reintroduced one layer over.

---

## 2. Root cause category — and why it is *not* the alternatives

**Category: N+1 query.** Justification is the linear scaling: doubling the orders
roughly doubles both the query count and the latency, tracking `1 + 3N`.

- **Not a missing index.** A missing index inflates the cost of a *single*
  statement; here every statement is already fast and there are *hundreds* of
  them. The composite index already exists.
- **Not serializer/Python CPU overhead.** Summing item totals in Python for a few
  hundred orders is microseconds of CPU. The 30s is spent **waiting on network
  round-trips to the DB**, not computing — the trace is I/O-bound, not CPU-bound.
- **Not a cache miss/stampede.** No cache sits in front of this path, and a cache
  problem would not produce a per-order fan-out of distinct parameterized SELECTs.

---

## 3. The Django/DB mechanisms involved (and why the fix works)

- **Lazy QuerySet evaluation & QuerySet caching.** `Order.objects.all()` doesn't
  hit the DB until iterated. Crucially, the *related* managers (`obj.items`) are
  separate querysets with their own caches — evaluating the outer queryset does
  **not** populate them.
- **Related-object descriptors.** `obj.customer` is a forward-FK descriptor; on a
  non-`select_related` row it lazily fires a `SELECT` on first access. That's one
  extra query per order in `get_customer_name`.
- **`.count()` issues `SELECT COUNT(*)`.** `obj.items.count()` runs a fresh
  aggregate query per order rather than counting in Python.
- **Materializing a reverse relation.** `obj.items.all()` in `get_total_amount`
  pulls every `OrderItem` row for that order — another query each.

**Why the fix (`FixedOrderSummaryView` + `FixedOrderSummarySerializer`) works:**
- **`select_related("customer")`** — customer is a to-one FK, so this becomes a
  single SQL **JOIN** resolved inside the one orders query; `customer.name` then
  reads from the already-fetched row with no extra query.
- **`.annotate(item_count=Count("items", distinct=True), total_amount=Coalesce(Sum(F("items__quantity") * F("items__unit_price")), 0))`**
  — pushes the counting and revenue math into the DB as **GROUP BY aggregates**,
  computed in the same query. No `OrderItem` rows are shipped to Python.
  (`prefetch_related` — a second query + Python-side join — would also collapse
  the to-many fan-out to a constant, but annotation is cheaper here because we
  only need aggregates, not the item rows.)

Result: query count drops from `1 + 3N` to a small **constant (~1)**, independent
of order count. Latency returns to ~80ms for any account size.

---

## 4. Preventing recurrence
- **`assertNumQueries(N)`** in tests around the summary view, so a serializer
  change that reintroduces per-row queries fails CI instead of production.
- **`django-silk`** enabled in CI/staging to record and diff per-request query
  counts (the two views are exposed side by side precisely so this is visible).
- **`nplusone`** in-process detector during tests/dev to flag unprefetched
  relation access as it happens.
- Code-review checklist item: any new `SerializerMethodField` crossing a relation
  must be paired with `select_related`/`prefetch_related`/`annotate` on the view.
