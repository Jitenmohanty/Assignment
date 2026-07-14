"""Helper to mint the demo tenant JWT (used by tests and the README curl demo)."""

import datetime

import jwt
from django.conf import settings


def make_tenant_token(slug, ttl_seconds=3600):
    """Return a short-lived signed JWT carrying the tenant slug claim.

    The ``exp`` claim bounds the token's lifetime so a leaked token cannot be
    replayed forever; the middleware rejects tokens that lack a valid ``exp``.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode(
        {"tenant": slug, "exp": now + datetime.timedelta(seconds=ttl_seconds)},
        settings.TENANT_JWT_SECRET,
        algorithm=settings.TENANT_JWT_ALGORITHM,
    )
