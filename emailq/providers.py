"""Simulated third-party email provider (Section 2).

Stands in for SendGrid/SES etc. Its behaviour is driven by fields on the job so
tests can force deterministic transient failures and permanent failures:

  * ``_fail_times``  : fail transiently this many times, then succeed.
  * ``_always_fail`` : raise a permanent (non-retryable) error every time.

Every *attempt* is logged to a Redis sorted set (score = ms timestamp) so a test
can independently verify the provider was never called more than N times in any
window — without trusting the rate limiter's own bookkeeping.
"""

import time
import uuid

import redis
from django.conf import settings


class TransientEmailError(Exception):
    """Temporary provider failure — safe to retry (e.g. 429/503/timeout)."""


class PermanentEmailError(Exception):
    """Non-retryable failure — bad recipient, malformed payload (4xx)."""


class SimulatedEmailProvider:
    def __init__(self, redis_client=None, attempts_log_key="emailq:provider:attempts"):
        self.redis = redis_client or redis.Redis.from_url(settings.REDIS_URL)
        self.attempts_log_key = attempts_log_key
        # per-job transient failure counters live in Redis so they survive
        # across separate task attempts / worker processes.
        self._fail_counter_prefix = "emailq:provider:failcount:"

    def send(self, job, now_ms=None):
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        # Record the attempt against the provider (independent audit trail).
        self.redis.zadd(self.attempts_log_key, {uuid.uuid4().hex: now_ms})

        if job.get("_always_fail"):
            raise PermanentEmailError(f"permanent failure for {job.get('to')}")

        fail_times = int(job.get("_fail_times", 0))
        if fail_times:
            counter_key = self._fail_counter_prefix + str(job["job_id"])
            seen = self.redis.incr(counter_key)
            if seen <= fail_times:
                raise TransientEmailError(
                    f"transient failure {seen}/{fail_times} for {job.get('to')}"
                )
        # Success.
        return {"provider_message_id": uuid.uuid4().hex}

    def attempts_in_window(self, window_ms, now_ms=None):
        """Max provider attempts within any trailing window ending at now."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return self.redis.zcount(self.attempts_log_key, now_ms - window_ms, now_ms)
