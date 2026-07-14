# Artikate Studio — Backend Developer Assessment

Python · Django 5.1 · DRF · Celery · Redis · django-silk

A single Django project with one app per section:

| App        | Section | What it demonstrates |
| ---------- | ------- | -------------------- |
| `orders`   | **1**   | Diagnosing & fixing an N+1 query regression (with silk evidence) |
| `emailq`   | **2**   | Rate-limited async job queue (Celery + Redis, custom sliding-window limiter) |
| `tenancy`  | **3**   | Automatic multi-tenant data isolation at the ORM layer (contextvars) |

### Where the written work lives
- **`docs/section1/INVESTIGATION.md`** — Section 1 incident investigation log
- **`docs/section1/silk-evidence.md`** — Section 1 query-count evidence (601 → 1)
- **`DESIGN.md`** — Section 2 architecture decisions & trade-offs
- **`ANSWERS.md`** — all written answers (Section 2 SIGKILL, Section 3 async, Section 4 A/B/C)

---

## Prerequisites

- Python 3.10+ (developed on 3.12)
- **Redis** running locally on `127.0.0.1:6379` (used as Celery broker **and** by the rate limiter)

Start Redis if it isn't already up:

```bash
redis-server --daemonize yes    # or: docker run -p 6379:6379 -d redis:7
redis-cli ping                  # -> PONG
```

---

## Setup (under 5 minutes)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

## Run all tests (clean environment)

```bash
python manage.py test
```

Expected: **28 tests, all passing.** (Section 2 tests need Redis running.)

Run a single section's tests:

```bash
python manage.py test orders     # Section 1
python manage.py test emailq     # Section 2
python manage.py test tenancy    # Section 3
```

---

## Section 1 — See the N+1 and the fix in django-silk

Regenerate the before/after query-count evidence from a clean state:

```bash
python manage.py capture_silk_evidence --orders 200
```

Output (also written to `docs/section1/silk-evidence.md`):

```
BROKEN /api/orders/summary/broken/  -> 601 SQL queries     (1 + 3N, N=200)
FIXED  /api/orders/summary/         -> 1  SQL queries
```

To explore interactively in the browser:

```bash
python manage.py seed_orders --orders 250
python manage.py runserver
# then open:
#   http://127.0.0.1:8000/api/orders/summary/broken/?customer_id=1   (slow, many queries)
#   http://127.0.0.1:8000/api/orders/summary/?customer_id=1          (fast, ~1 query)
#   http://127.0.0.1:8000/silk/   -> compare "queries" column per request
```

---

## Section 2 — Run the rate-limited email queue

The core behaviour is fully proven by the test suite (see
`emailq/tests.py::FiveHundredJobsTests` — 500 jobs, no loss, rate never exceeded,
retries + dead-letter). To watch it live with a real worker:

```bash
# Terminal 1 — start a Celery worker
celery -A config worker -l info

# Terminal 2 — simulate a flash-sale burst
python manage.py submit_emails --count 2000 --fail-every 50

# Inspect Redis state live
redis-cli ZCARD emailq:ratelimit:email      # admissions in the current window (<= 200)
redis-cli LLEN  emailq:dead_letter          # permanently-failed jobs
```

The worker drains the backlog at **≤ 200 emails / 60s**, retries transient
failures with exponential backoff, and dead-letters permanent ones. Key config
(crash safety) is in `config/settings.py`: `task_acks_late`,
`task_reject_on_worker_lost`, `worker_prefetch_multiplier = 1`.

---

## Section 3 — Multi-tenant isolation

Proven by `tenancy/tests.py` (negative tests: tenant A cannot reach tenant B's
rows; `.objects.all()` is scoped; concurrent async tasks don't leak context). To
try the middleware live:

```bash
python manage.py shell -c "
from tenancy.models import Tenant, Order
from tenancy.tokens import make_tenant_token
a = Tenant.objects.create(name='Acme', slug='acme')
Order.all_tenants.create(tenant=a, description='hello')
print('JWT for acme:', make_tenant_token('acme'))
"
python manage.py runserver

# JWT header:
curl -H "Authorization: Bearer <token-from-above>" http://127.0.0.1:8000/api/tenancy/whoami/
# Subdomain (Host header):
curl -H "Host: acme.artikate.test" http://127.0.0.1:8000/api/tenancy/whoami/
```

`whoami` calls `Order.objects.count()` with **no explicit tenant filter**, yet
only ever counts the current tenant's rows.

---

## Notes

- No secrets are committed. `SECRET_KEY` and other config read from env vars with
  safe dev fallbacks (see `config/settings.py`); `.env`/`db.sqlite3` are gitignored.
- SQLite is used for zero-setup local review. Nothing in the code is
  SQLite-specific except where noted; the N+1 fix and tenant scoping are
  database-agnostic.
- The optional Section 5 screen recording was not produced; Section 2 is instead
  fully demonstrated via the live commands above and the automated 500-job test.
