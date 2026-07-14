"""Celery-agnostic delivery core + shared helpers (Section 2).

Keeping the delivery logic out of the Celery task lets us test the real rate
limiter, provider, backoff and dead-letter behaviour deterministically with a
virtual clock — without a live broker and without Celery's eager-mode retry
quirks. The Celery task in ``tasks.py`` is a thin translation layer on top.
"""

from dataclasses import dataclass

import redis
from django.conf import settings

from .providers import (PermanentEmailError, SimulatedEmailProvider,
                        TransientEmailError)
from .rate_limiter import RedisSlidingWindowRateLimiter

# Delivery attempt outcomes.
SENT = "sent"
THROTTLED = "throttled"        # rate limit hit — retry later, NOT a failure
TRANSIENT_FAILURE = "transient_failure"   # provider hiccup — retry with backoff
PERMANENT_FAILURE = "permanent_failure"   # never retry — dead-letter immediately

RATE_LIMIT_KEY = "emailq:ratelimit:email"
DEAD_LETTER_KEY = "emailq:dead_letter"

# Exponential backoff: base * 2**retries, capped, used by the Celery task.
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 600  # 10 min ceiling


@dataclass
class AttemptResult:
    status: str
    retry_after: float = 0.0   # seconds (throttle)
    detail: str = ""


def get_redis():
    return redis.Redis.from_url(settings.REDIS_URL)


def get_limiter(redis_client=None, key=RATE_LIMIT_KEY, limit=None, window=None):
    return RedisSlidingWindowRateLimiter(
        redis_client or get_redis(),
        key=key,
        limit=limit if limit is not None else settings.EMAIL_RATE_LIMIT,
        window_seconds=window if window is not None
        else settings.EMAIL_RATE_WINDOW_SECONDS,
        fail_open=settings.RATE_LIMIT_FAIL_OPEN,
    )


def get_provider(redis_client=None):
    return SimulatedEmailProvider(redis_client or get_redis())


def backoff_seconds(retries):
    """Exponential backoff with a hard ceiling. Jitter is added by the caller."""
    return min(BACKOFF_BASE_SECONDS * (2 ** retries), BACKOFF_MAX_SECONDS)


def attempt_delivery(job, limiter, provider, now_ms=None):
    """Make exactly one delivery attempt. Never sleeps, never retries.

    The caller inspects the returned status and decides whether to reschedule
    (throttle / transient) or dead-letter (permanent).
    """
    # Rate gate first: if we can't send now, don't even call the provider.
    allowed, retry_after = limiter.acquire(now_ms=now_ms)
    if not allowed:
        return AttemptResult(THROTTLED, retry_after=retry_after)

    try:
        provider.send(job, now_ms=now_ms)
    except PermanentEmailError as exc:
        return AttemptResult(PERMANENT_FAILURE, detail=str(exc))
    except TransientEmailError as exc:
        return AttemptResult(TRANSIENT_FAILURE, detail=str(exc))
    return AttemptResult(SENT)


def dead_letter(job, reason, redis_client=None):
    """Persist a permanently-failed job so it is never silently lost.

    Uses a Redis list (LPUSH) as a durable dead-letter queue that ops can
    inspect, drain, or replay. Kept separate from the Celery broker so a broker
    purge never discards evidence of failures.
    """
    import json
    r = redis_client or get_redis()
    r.lpush(DEAD_LETTER_KEY, json.dumps({"job": job, "reason": reason}))


def dead_letter_size(redis_client=None):
    return (redis_client or get_redis()).llen(DEAD_LETTER_KEY)
