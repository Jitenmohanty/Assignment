"""Helper to mint the demo tenant JWT (used by tests and the README curl demo)."""

import jwt
from django.conf import settings


def make_tenant_token(slug):
    """Return a signed JWT carrying the tenant slug claim."""
    return jwt.encode(
        {"tenant": slug},
        settings.TENANT_JWT_SECRET,
        algorithm=settings.TENANT_JWT_ALGORITHM,
    )
