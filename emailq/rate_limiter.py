"""Redis sliding-window rate limiter (Section 2).

Enforces "no more than ``limit`` events per ``window`` seconds" precisely — the
count is over a *trailing* window, not a fixed calendar window, so it cannot be
gamed by the boundary burst that Option C (fixed window INCR+EXPIRE) allows.

Data structure: one Redis sorted set per limiter key.
  * member = a unique id for the admission
  * score  = the admission timestamp in milliseconds

Each acquire() runs a single Lua script that atomically:
  1. ZREMRANGEBYSCORE  — drops entries older than (now - window)
  2. ZCARD             — counts what remains in the window
  3. if under limit: ZADD the new entry + PEXPIRE the key, return allowed
     else: read the oldest entry's score, return the exact retry-after

Atomicity: the whole read-modify-write is ONE Lua script, so Redis runs it
without interleaving other clients. Two workers calling acquire() concurrently
can never both see "count = 199" and both admit — the second sees 200. This is
the guarantee a plain GET/INCR pair or a Python-side check cannot make.
"""

import time
import uuid

import redis

# KEYS[1] = limiter key
# ARGV[1] = now_ms, ARGV[2] = window_ms, ARGV[3] = limit, ARGV[4] = member id
# returns {allowed(0|1), retry_after_ms}
_SLIDING_WINDOW_LUA = """
local key      = KEYS[1]
local now      = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local limit    = tonumber(ARGV[3])
local member   = ARGV[4]

-- 1. evict entries that have aged out of the trailing window
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 2. how many admissions remain inside the window
local count = redis.call('ZCARD', key)

if count < limit then
    -- 3a. room available: record this admission
    redis.call('ZADD', key, now, member)
    -- keep the key from living forever once traffic stops
    redis.call('PEXPIRE', key, window)
    return {1, 0}
else
    -- 3b. full: caller must wait until the oldest entry leaves the window
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest[2] then
        retry_after = (tonumber(oldest[2]) + window) - now
        if retry_after < 0 then retry_after = 0 end
    end
    return {0, retry_after}
end
"""


class RedisSlidingWindowRateLimiter:
    def __init__(self, redis_client, key, limit, window_seconds, fail_open=True):
        self.redis = redis_client
        self.key = key
        self.limit = int(limit)
        self.window_ms = int(window_seconds * 1000)
        # fail_open: on a Redis outage, admit the event rather than block all
        # sending. Right default for transactional email — the provider's own
        # 429 + our exponential backoff remain a second line of defence. Flip to
        # False for bulk/marketing mail where breaching the cap has real cost.
        self.fail_open = fail_open
        # register_script uses EVALSHA with an EVAL fallback — the script is
        # cached in Redis, only the SHA travels on subsequent calls.
        self._script = redis_client.register_script(_SLIDING_WINDOW_LUA)

    def acquire(self, now_ms=None, member=None):
        """Try to admit one event.

        Returns ``(allowed: bool, retry_after_seconds: float)``. Never sleeps;
        the caller decides how to wait (Celery reschedules with a countdown).

        ``now_ms`` is injectable so tests can drive a virtual clock; in
        production it defaults to wall-clock time.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if member is None:
            member = uuid.uuid4().hex
        try:
            allowed, retry_after_ms = self._script(
                keys=[self.key],
                args=[now_ms, self.window_ms, self.limit, member],
            )
        except redis.RedisError:
            # Redis is the enforcement mechanism; if it is down we cannot make a
            # decision. Fail open (admit) or closed (block) per configuration.
            if self.fail_open:
                return True, 0.0
            raise
        return bool(allowed), max(0.0, retry_after_ms / 1000.0)

    def current_count(self, now_ms=None):
        """Admissions currently inside the trailing window (for tests/metrics)."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        self.redis.zremrangebyscore(self.key, 0, now_ms - self.window_ms)
        return self.redis.zcard(self.key)
