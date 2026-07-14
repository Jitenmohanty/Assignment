# Section 2 — Rate-Limited Async Email Queue: Design & Trade-offs

## Problem framing

We send transactional email (OTPs, order confirmations) through a third-party
provider capped at **200 emails/minute**. During flash sales we see bursts of
~**2000 requests in under 10 seconds** — a 10x overshoot of the provider's
per-window budget compressed into a fraction of a window. The system must:
never exceed the provider cap, retry on failure, and lose no job if a worker is
SIGKILL'd mid-run. The interesting engineering is not "make it async" — it's the
tension between admitting a burst quickly and never breaching a hard external
limit, while being safe against process death.

---

## 1. Architecture choice — Celery + Redis vs Django Q vs custom

**Celery + Redis.** Mature, battle-tested, and — decisively for this workload —
it gives us the two crash-safety primitives we need as first-class settings:
`task_acks_late` and `task_reject_on_worker_lost`, plus native retry with
`countdown`/`max_retries` that we lean on directly in `tasks.py`. Rich ecosystem
(Flower for monitoring, autoscaling, routing, pluggable brokers). Cost: it is
heavy. It brings kombu/billiard, a non-trivial config surface, and a worker
model you must actually operate. With **Redis as broker specifically**, delivery
guarantees hinge on the *visibility timeout* — a task that outlives the timeout
can be re-delivered even without a crash, so long-running tasks need the timeout
tuned above their worst-case runtime. RabbitMQ gives cleaner ack semantics and
per-message delivery tracking, but adds a second stateful service to run when we
already need Redis for the rate limiter. Sharing one Redis for broker, result
backend, and limiter is the pragmatic call here.

**Django Q.** Lighter, ORM-integrated, pleasant for small/medium async needs and
low operational overhead — the broker can be the database itself. But its feature
set is thinner: retry/backoff and crash-recovery semantics are less granular than
Celery's `acks_late` + `reject_on_worker_lost`, monitoring tooling is weaker, and
under a 2000-in-10s burst the DB-as-broker path becomes a contention point.
Fine for cron-style jobs; underspec'd for a hard-rate-limited burst funnel.

**Custom (Redis lists / RQ-style loop).** Maximum control, minimum dependency
weight, and we'd understand every line. But we would be re-implementing exactly
the parts that are hard to get right: at-least-once redelivery on crash, retry
scheduling with backoff, visibility timeouts, and observability. That is a lot of
subtle concurrency code to own for no differentiated benefit.

**Decision.** Celery + Redis. It fits a bursty, retry-heavy, crash-sensitive
transactional workload: late-ack redelivery for crash safety, built-in
exponential-backoff retries, and a mature monitoring story. The assessment
mandated this stack; we **agree with the mandate** and the reasoning above stands
on its own. Honest downsides we accept: real operational complexity, and the
Redis-broker visibility-timeout caveat — we mitigate by keeping tasks short
(one delivery attempt each, never sleeping in-task) so runtime stays well under
the timeout.

---

## 2. Rate limiter — why a sliding-window log (Redis sorted set)

The requirement is a **hard ceiling**: "never more than 200 in any 60s." That
phrasing is what selects the algorithm.

- **(A) Token bucket (`DECR` + TTL refill).** O(1) state, cheap, and the natural
  choice when you *want* controlled bursting. But it admits bursts by design (up
  to bucket capacity), and enforcing a strict trailing-window ceiling requires
  careful refill-rate math and clock handling. It answers "what average rate,
  with how much burst" — not "no more than N in *any* trailing window."
- **(C) Fixed window (`INCR` + `EXPIRE`).** Simplest and O(1), but it has the
  **boundary-burst flaw**: a client can send 200 in the last second of one
  calendar window and 200 in the first second of the next — **up to ~400 within a
  single rolling 60s span**. For a provider that will 429 us at 200, that is a
  guaranteed breach exactly during a flash-sale spike.
- **(B) Sliding-window log (sorted set).** One ZSET member per admission, score =
  admission timestamp in ms. `ZREMRANGEBYSCORE` evicts everything older than
  `now - window`, `ZCARD` counts what remains. This is the most precise possible
  expression of "no more than N in any trailing 60s," and it is **trivially
  auditable** — the set literally *is* the list of the last minute's admissions,
  and `providers.py` keeps an independent attempt log so tests verify the cap
  without trusting the limiter's own bookkeeping.

**Decision.** Sliding-window log (B). The requirement is a strict ceiling, not a
rate-with-burst, so we optimize for correctness at the boundary and for
auditability. **Trade-off we accept:** the log is **O(N) memory per window** —
one member for every admission (200 here), plus O(log N) ZADD/ZREM. At 200/min
this is negligible. At very high volume (tens of thousands per window per key)
the per-member overhead would matter and **token bucket's O(1) state would win** —
that is the point at which we'd revisit. It also supplies an *exact* retry-after
(oldest entry's score + window − now), so throttled tasks reschedule to the
precise moment a slot frees, rather than polling.

---

## 3. Atomicity guarantee

The evict → count → conditional-add is **one Lua script**
(`_SLIDING_WINDOW_LUA`), registered via `register_script` (EVALSHA with an EVAL
fallback). Redis is single-threaded for script execution, so the whole
read-modify-write runs **without interleaving** any other client's commands.
Concretely: two workers calling `acquire()` at the same instant when count is 199
**cannot both admit** — the script that runs first does `ZADD` and the second one
`ZCARD`s 200 and is rejected. There is no window between the check and the write.

A naive `GET`-then-`INCR`, or reading the count into Python and deciding there,
**races**: both workers read 199, both admit, and we breach the provider cap
exactly under the concurrent load we're trying to protect against. This
race is precisely why we did **not** just wrap a third-party limiter or split the
logic across multiple round-trips — the atomicity has to live server-side in a
single script, and correctness under concurrency was worth owning that ~25 lines
of Lua.

---

## 4. Redis failure — fail open or fail closed?

**Implemented behavior:** `acquire()` wraps the script call in
`try/except redis.RedisError`. The policy is controlled by the `fail_open` flag
(from `RATE_LIMIT_FAIL_OPEN`, default **True**): on a Redis outage we either
return `(True, 0.0)` and keep sending (fail open) or re-raise and block (fail
closed). It is a **conscious, configurable** decision, not an accident of
exception propagation.

The trade-off, stated plainly:

- **Fail closed:** we can never breach the provider cap, but *all* sending stalls
  during a Redis outage. For transactional mail that means OTPs and
  order-confirmations don't arrive — a direct, user-visible UX/trust hit, and
  arguably worse than the thing we're protecting against.
- **Fail open:** email keeps flowing during a Redis outage. We risk exceeding
  200/min, but that risk is **already absorbed downstream**: the provider returns
  429, our provider layer raises `TransientEmailError`, and the Celery task
  retries with exponential backoff + full jitter. In other words the provider's
  own limit plus our backoff is a working second line of defense; the Redis
  limiter is an *optimization* that avoids wasting attempts on calls we know will
  be throttled, not the sole enforcement mechanism.

**Recommendation (and what we shipped).** For *transactional* email we default
**fail-open** (`RATE_LIMIT_FAIL_OPEN=1`), relying on the provider's 429 + our
backoff as the safety net during a Redis outage. In production you would also emit
a metric/log on the except branch so a silent Redis outage is loud in monitoring.
If this were *bulk/marketing* mail where breaching the cap has real cost and
delayed delivery is harmless, set `RATE_LIMIT_FAIL_OPEN=0` — the right answer
depends on the value of the message, so it lives behind a flag, not hard-coded.

---

## 5. Crash safety / SIGKILL (summary; full write-up in ANSWERS.md)

Three settings combine to make an in-flight job survive a hard kill:

- `task_acks_late=True` — a task is acked **after** it returns, not on pickup.
- `task_reject_on_worker_lost=True` — if the worker process dies (not a normal
  exception), the task is rejected back to the broker rather than acked.
- `worker_prefetch_multiplier=1` — a worker holds at most one un-acked task, so a
  SIGKILL loses **at most one** in-flight job, not a prefetched batch.

A SIGKILL'd worker never sends the ack; Redis's **visibility-timeout** redelivery
then hands the job to another worker. The consequence is **at-least-once**
delivery: a job can run more than once (killed after `provider.send` but before
ack). Tasks must therefore be **idempotent** — the same email should not be sent
twice — which is the trade-off we take for zero job loss. (Full analysis of the
kill-timing window and idempotency approach is in ANSWERS.md.)

---

## 6. Dead-letter handling

Permanently-failed jobs — a `PermanentEmailError` (bad recipient / malformed
payload, non-retryable) or a transient failure that **exhausts `max_retries`** —
are `LPUSH`'d to the Redis list `emailq:dead_letter` by `dead_letter()` in
`service.py`. Two deliberate choices:

- **Separate from the broker.** The dead-letter list is not the Celery queue, so
  a broker purge (a routine ops action during incident cleanup) never discards
  the evidence of what failed and why. Each entry stores the full job plus a
  human-readable reason.
- **Inspectable and replayable.** Ops can `LLEN`/`LRANGE` to triage, and because
  the original job payload is preserved, a drained entry can be re-enqueued after
  the root cause (bad template, provider outage, address typo) is fixed.

Note one distinction baked into `tasks.py`: **throttling is not a failure.**
Throttled tasks retry with `max_retries=None` and never dead-letter — a long
flash-sale backlog just keeps rescheduling against the limiter's exact
retry-after until the window drains. Only genuine failures consume the finite
`MAX_FAILURE_RETRIES` budget and end up in the dead-letter list. This keeps the
dead-letter queue a true signal of *broken* mail, not merely *delayed* mail.
