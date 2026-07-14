"""Section 2 tests.

Layers:
  * RateLimiterTests      — the Redis sliding-window limiter in isolation,
                            including a concurrency/atomicity check.
  * DeliveryCoreTests     — attempt_delivery outcome mapping.
  * FiveHundredJobsTests  — the required end-to-end test: submit 500 jobs and
                            assert no loss, rate never exceeded, retries work,
                            permanent + exhausted failures dead-letter.
  * CeleryTaskWiringTests — the Celery task itself (via .apply(), no broker).

All tests use the real Redis that Celery/the limiter use, and clean their keys.
"""

import heapq

import redis
from django.conf import settings
from django.test import TestCase, override_settings

from .providers import SimulatedEmailProvider
from .rate_limiter import RedisSlidingWindowRateLimiter
from .service import (PERMANENT_FAILURE, SENT, THROTTLED, TRANSIENT_FAILURE,
                      attempt_delivery, dead_letter_size, get_limiter)

TEST_PREFIX = "emailq:test:"


class RedisKeyCleanupMixin:
    def setUp(self):
        self.r = redis.Redis.from_url(settings.REDIS_URL)
        self._flush()
        self.addCleanup(self._flush)

    def _flush(self):
        for pattern in (TEST_PREFIX + "*", "emailq:ratelimit:*",
                        "emailq:provider:*", "emailq:dead_letter"):
            keys = list(self.r.scan_iter(match=pattern))
            if keys:
                self.r.delete(*keys)


class RateLimiterTests(RedisKeyCleanupMixin, TestCase):
    def _limiter(self, limit=5, window=10):
        return RedisSlidingWindowRateLimiter(
            self.r, key=TEST_PREFIX + "rl", limit=limit, window_seconds=window
        )

    def test_admits_exactly_limit_then_denies(self):
        rl = self._limiter(limit=5, window=10)
        now = 1_000_000
        results = [rl.acquire(now_ms=now)[0] for _ in range(6)]
        self.assertEqual(results, [True, True, True, True, True, False])

    def test_denied_returns_positive_retry_after(self):
        rl = self._limiter(limit=1, window=10)
        now = 1_000_000
        self.assertTrue(rl.acquire(now_ms=now)[0])
        allowed, retry_after = rl.acquire(now_ms=now)
        self.assertFalse(allowed)
        # Oldest entry at `now`; it leaves the 10s window after ~10s.
        self.assertAlmostEqual(retry_after, 10.0, delta=0.01)

    def test_window_slides_and_readmits(self):
        rl = self._limiter(limit=2, window=10)
        start = 1_000_000
        self.assertTrue(rl.acquire(now_ms=start)[0])
        self.assertTrue(rl.acquire(now_ms=start)[0])
        self.assertFalse(rl.acquire(now_ms=start)[0])
        # Advance past the window; the two old entries age out.
        later = start + 10_001
        self.assertTrue(rl.acquire(now_ms=later)[0])
        self.assertTrue(rl.acquire(now_ms=later)[0])
        self.assertFalse(rl.acquire(now_ms=later)[0])

    def test_atomic_under_concurrency(self):
        """20 threads hammer a limit-of-10 window; never more than 10 admitted.

        This is the property a Python-side check-then-set would violate.
        """
        import threading
        rl = self._limiter(limit=10, window=60)
        now = 2_000_000
        admitted = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()  # maximise real contention
            ok, _ = rl.acquire(now_ms=now)
            if ok:
                with lock:
                    admitted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(admitted), 10)

    def test_fails_open_on_redis_error(self):
        """Redis down + fail_open=True => admit (transactional default)."""
        bad = redis.Redis(host="127.0.0.1", port=1)  # nothing listening
        rl = RedisSlidingWindowRateLimiter(
            bad, key=TEST_PREFIX + "rl", limit=5, window_seconds=10,
            fail_open=True)
        allowed, retry_after = rl.acquire(now_ms=1)
        self.assertTrue(allowed)
        self.assertEqual(retry_after, 0.0)

    def test_fails_closed_on_redis_error_when_configured(self):
        """Redis down + fail_open=False => raise rather than silently admit."""
        bad = redis.Redis(host="127.0.0.1", port=1)
        rl = RedisSlidingWindowRateLimiter(
            bad, key=TEST_PREFIX + "rl", limit=5, window_seconds=10,
            fail_open=False)
        with self.assertRaises(redis.RedisError):
            rl.acquire(now_ms=1)


class DeliveryCoreTests(RedisKeyCleanupMixin, TestCase):
    def _setup(self, limit=100):
        limiter = get_limiter(self.r, key=TEST_PREFIX + "rl", limit=limit,
                              window=60)
        provider = SimulatedEmailProvider(
            self.r, attempts_log_key=TEST_PREFIX + "attempts")
        return limiter, provider

    def test_sent(self):
        limiter, provider = self._setup()
        r = attempt_delivery({"job_id": "1", "to": "a@x.com"}, limiter, provider)
        self.assertEqual(r.status, SENT)

    def test_throttled_when_limit_exhausted(self):
        limiter, provider = self._setup(limit=1)
        attempt_delivery({"job_id": "1", "to": "a@x.com"}, limiter, provider)
        r = attempt_delivery({"job_id": "2", "to": "b@x.com"}, limiter, provider)
        self.assertEqual(r.status, THROTTLED)
        self.assertGreater(r.retry_after, 0)

    def test_transient_failure(self):
        limiter, provider = self._setup()
        r = attempt_delivery(
            {"job_id": "1", "to": "a@x.com", "_fail_times": 1}, limiter, provider)
        self.assertEqual(r.status, TRANSIENT_FAILURE)

    def test_permanent_failure(self):
        limiter, provider = self._setup()
        r = attempt_delivery(
            {"job_id": "1", "to": "a@x.com", "_always_fail": True},
            limiter, provider)
        self.assertEqual(r.status, PERMANENT_FAILURE)


class FiveHundredJobsTests(RedisKeyCleanupMixin, TestCase):
    """The required test: 500 jobs through the real limiter + provider.

    Celery's redelivery (``self.retry(countdown=...)``) is simulated by a
    virtual-clock due-time heap, so the test is deterministic and fast while
    exercising the real rate limiter and delivery core. This mirrors exactly
    what a worker does — reschedule the job for later — without a live broker.
    """

    LIMIT = 50
    WINDOW_MS = 1000
    MAX_RETRIES = 5

    def test_500_jobs_no_loss_rate_respected_retries_work(self):
        limiter = RedisSlidingWindowRateLimiter(
            self.r, key=TEST_PREFIX + "rl", limit=self.LIMIT,
            window_seconds=self.WINDOW_MS / 1000)
        provider = SimulatedEmailProvider(
            self.r, attempts_log_key=TEST_PREFIX + "attempts")

        jobs = self._build_jobs()
        all_ids = {j["job_id"] for j in jobs}

        # Virtual-clock scheduler: (due_ms, seq, job). All jobs due at t=0.
        heap = [(0, i, j) for i, j in enumerate(jobs)]
        heapq.heapify(heap)
        seq = len(jobs)

        now = 0
        sent, dead = set(), set()
        attempts_per_job = {}
        max_in_window = 0
        guard = 0

        while heap:
            guard += 1
            self.assertLess(guard, 500_000, "scheduler failed to converge")

            due, _, job = heapq.heappop(heap)
            if due > now:
                now = due  # jump the virtual clock to the next due job

            res = attempt_delivery(job, limiter, provider, now_ms=now)
            jid = job["job_id"]

            if res.status == SENT:
                attempts_per_job[jid] = attempts_per_job.get(jid, 0) + 1
                sent.add(jid)
                max_in_window = max(
                    max_in_window, limiter.current_count(now_ms=now))
            elif res.status == THROTTLED:
                # reschedule exactly like self.retry(countdown=retry_after)
                heapq.heappush(
                    heap, (now + int(res.retry_after * 1000) + 1, seq, job))
                seq += 1
            elif res.status == TRANSIENT_FAILURE:
                attempts_per_job[jid] = attempts_per_job.get(jid, 0) + 1
                job["_retries"] = job.get("_retries", 0)
                if job["_retries"] >= self.MAX_RETRIES:
                    dead.add(jid)  # exhausted -> dead-letter
                else:
                    job["_retries"] += 1
                    heapq.heappush(heap, (now + 10, seq, job))  # small backoff
                    seq += 1
            elif res.status == PERMANENT_FAILURE:
                attempts_per_job[jid] = attempts_per_job.get(jid, 0) + 1
                dead.add(jid)

        # --- Assertion 1: no job is lost ---
        self.assertEqual(sent | dead, all_ids)
        self.assertEqual(len(sent) + len(dead), 500)

        # --- Assertion 2: rate limit never exceeded (independent audit) ---
        self.assertLessEqual(max_in_window, self.LIMIT)
        self._assert_provider_never_exceeded(provider)

        # --- Assertion 3: intentional transient failures retried, then sent ---
        retried_and_sent = [
            jid for jid in sent
            if jid.startswith("fail-once") and attempts_per_job[jid] >= 2
        ]
        self.assertGreaterEqual(len(retried_and_sent), 1)
        self.assertTrue(all(f"fail-once-{i}" in sent for i in range(10)))

        # --- Assertion 4: permanent + exhausted-transient jobs dead-lettered ---
        self.assertEqual(len(dead), 4)  # 2 always-fail + 2 always-transient
        for i in range(2):
            self.assertIn(f"perm-{i}", dead)
            self.assertIn(f"transient-forever-{i}", dead)

    def _build_jobs(self):
        jobs = []
        for i in range(486):
            jobs.append({"job_id": f"ok-{i}", "to": f"ok{i}@x.com"})
        for i in range(10):
            jobs.append({"job_id": f"fail-once-{i}", "to": f"f{i}@x.com",
                        "_fail_times": 1})
        for i in range(2):
            jobs.append({"job_id": f"transient-forever-{i}", "to": f"t{i}@x.com",
                        "_fail_times": 99})
        for i in range(2):
            jobs.append({"job_id": f"perm-{i}", "to": f"p{i}@x.com",
                        "_always_fail": True})
        assert len(jobs) == 500
        return jobs

    def _assert_provider_never_exceeded(self, provider):
        entries = self.r.zrange(provider.attempts_log_key, 0, -1, withscores=True)
        scores = sorted(int(s) for _, s in entries)
        for t in scores:
            # count attempts in the trailing window (t - window, t]
            in_window = self.r.zcount(
                provider.attempts_log_key, t - self.WINDOW_MS + 1, t)
            self.assertLessEqual(
                in_window, self.LIMIT,
                f"rate exceeded: {in_window} attempts in window ending {t}")


@override_settings(EMAIL_RATE_LIMIT=1000, EMAIL_RATE_WINDOW_SECONDS=60)
class CeleryTaskWiringTests(RedisKeyCleanupMixin, TestCase):
    """Exercise the Celery task via .apply() (synchronous, no broker)."""

    def test_success_path_returns_sent(self):
        from .tasks import send_email
        res = send_email.apply(kwargs={"job_id": "ok-1", "to": "a@x.com",
                                       "subject": "hi", "body": "yo"})
        self.assertEqual(res.get()["status"], SENT)

    def test_permanent_failure_is_dead_lettered(self):
        from .tasks import send_email
        before = dead_letter_size()
        res = send_email.apply(kwargs={"job_id": "perm", "to": "a@x.com",
                                       "_always_fail": True})
        self.assertEqual(res.get()["status"], PERMANENT_FAILURE)
        self.assertEqual(dead_letter_size(), before + 1)
