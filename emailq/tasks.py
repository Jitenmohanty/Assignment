"""Celery task wiring for email delivery (Section 2).

The task translates an :func:`attempt_delivery` outcome into Celery control flow:

  * THROTTLED          -> retry after the limiter's retry_after (not a failure,
                          so it does not consume the failure retry budget).
  * TRANSIENT_FAILURE  -> retry with exponential backoff, up to max_retries,
                          then dead-letter.
  * PERMANENT_FAILURE  -> dead-letter immediately (no retry).
  * SENT               -> done.

Crash safety is configured globally in settings (task_acks_late=True,
task_reject_on_worker_lost=True, prefetch_multiplier=1): a task is ack'd only
after it returns, so a SIGKILL'd worker's in-flight job is re-delivered rather
than lost. See ANSWERS.md (Section 2) for the full explanation.
"""

import random

from celery import shared_task

from .service import (BACKOFF_MAX_SECONDS, PERMANENT_FAILURE, SENT, THROTTLED,
                      TRANSIENT_FAILURE, attempt_delivery, backoff_seconds,
                      dead_letter, get_limiter, get_provider)

# Separate ceiling for throttle retries so a long flash-sale backlog keeps
# rescheduling instead of dead-lettering perfectly good emails.
MAX_FAILURE_RETRIES = 5


@shared_task(
    bind=True,
    name="emailq.send_email",
    max_retries=MAX_FAILURE_RETRIES,
    acks_late=True,
)
def send_email(self, **job):
    """Deliver one transactional email, respecting the shared rate limit."""
    limiter = get_limiter()
    provider = get_provider()

    result = attempt_delivery(job, limiter=limiter, provider=provider)

    if result.status == SENT:
        return {"status": SENT, "job_id": job.get("job_id")}

    if result.status == THROTTLED:
        # Rate limited: reschedule without burning the failure budget.
        # countdown is the limiter's exact retry_after; no time.sleep involved.
        raise self.retry(
            countdown=max(1, round(result.retry_after)),
            max_retries=None,  # throttling is transient by nature, keep trying
        )

    if result.status == PERMANENT_FAILURE:
        dead_letter(job, reason=result.detail)
        return {"status": PERMANENT_FAILURE, "job_id": job.get("job_id")}

    # TRANSIENT_FAILURE: exponential backoff, then dead-letter on exhaustion.
    if self.request.retries >= self.max_retries:
        dead_letter(job, reason=f"max retries exceeded: {result.detail}")
        return {"status": "dead_letter", "job_id": job.get("job_id")}

    countdown = backoff_seconds(self.request.retries)
    # Full jitter to avoid a thundering herd of retries after a provider blip.
    countdown = min(BACKOFF_MAX_SECONDS, random.uniform(0, countdown) + 1)
    raise self.retry(countdown=countdown)
