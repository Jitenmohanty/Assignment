"""Per-request tenant context.

We store the active tenant in a :class:`contextvars.ContextVar`, NOT a
``threading.local``. contextvars is the right primitive here because:

  * In sync WSGI it behaves like thread-local (each thread gets its own value).
  * In async ASGI, a single OS thread interleaves many requests via the event
    loop. A thread-local would be *shared* by all coroutines on that thread, so
    request B could read request A's tenant. A ContextVar is copied per task
    (``asyncio`` runs each request in its own ``contextvars.copy_context()``),
    so the value follows the logical request, not the OS thread.

See ANSWERS.md (Section 3) for the full async failure-mode discussion.
"""

import contextvars
from contextlib import contextmanager

# The active tenant for the current execution context (request/task).
_current_tenant = contextvars.ContextVar("current_tenant", default=None)

# When True, TenantManager returns unscoped querysets. Used by trusted system
# code (admin jobs, cross-tenant reporting, migrations) that must opt in
# explicitly — never by accident.
_unscoped = contextvars.ContextVar("tenant_unscoped", default=False)


def set_current_tenant(tenant):
    """Bind ``tenant`` to the current context. Returns a reset token."""
    return _current_tenant.set(tenant)


def get_current_tenant():
    """Return the active tenant, or None if none is bound."""
    return _current_tenant.get()


def clear_current_tenant(token=None):
    """Restore the previous tenant value.

    Passing the token returned by :func:`set_current_tenant` restores the exact
    prior state (safe for nesting). Without a token we hard-reset to None.
    """
    if token is not None:
        _current_tenant.reset(token)
    else:
        _current_tenant.set(None)


def is_unscoped():
    return _unscoped.get()


@contextmanager
def unscoped():
    """Temporarily disable tenant scoping for trusted cross-tenant access.

    Usage:
        with unscoped():
            Order.objects.all()   # sees every tenant's rows
    """
    token = _unscoped.set(True)
    try:
        yield
    finally:
        _unscoped.reset(token)
