"""Tenant resolution middleware (Section 3).

Binds the tenant for the *entire* request lifecycle and — crucially — clears it
in a ``finally`` block so context never leaks to the next request served by the
same worker thread.

Two resolution strategies, tried in order:
  1. JWT in the ``Authorization: Bearer <token>`` header, with a ``tenant`` claim
     holding the tenant slug. (Typical for a mobile app / API.)
  2. Subdomain of the Host header, e.g. ``acme.artikate.test`` -> slug ``acme``.
     (Typical for a browser SaaS.)

Unknown/absent tenant is left as None; TenantManager then fails closed.
"""

import jwt
from django.conf import settings

from .context import clear_current_tenant, set_current_tenant
from .models import Tenant


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve_tenant(request)
        # set_current_tenant returns a token; resetting with it restores the
        # exact previous state, which is the correct cleanup for ContextVars.
        token = set_current_tenant(tenant)
        request.tenant = tenant
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant(token)

    # -- resolution ---------------------------------------------------------
    def _resolve_tenant(self, request):
        slug = self._slug_from_jwt(request) or self._slug_from_subdomain(request)
        if not slug:
            return None
        # all_tenants: middleware itself must look up across tenants to *find*
        # the tenant — it cannot be scoped by the thing it is trying to resolve.
        return Tenant.objects.filter(slug=slug).first()

    def _slug_from_jwt(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None
        token = header.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(
                token,
                settings.TENANT_JWT_SECRET,
                algorithms=[settings.TENANT_JWT_ALGORITHM],
            )
        except jwt.PyJWTError:
            return None
        return payload.get("tenant")

    def _slug_from_subdomain(self, request):
        host = request.get_host().split(":")[0]  # strip port
        base = settings.TENANT_BASE_DOMAIN
        if host == base or not host.endswith("." + base):
            return None
        # "acme.artikate.test" -> "acme"
        return host[: -(len(base) + 1)].split(".")[-1]
