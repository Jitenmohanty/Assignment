"""Automatic tenant scoping at the ORM layer.

``TenantManager.get_queryset`` is the single choke point through which
``Order.objects.all()``, ``.filter()``, ``.get()``, ``.count()`` — every read —
flows. By injecting ``.filter(tenant=...)`` here, scoping is applied whether or
not the caller remembers to. There is no code path in normal application use
that returns another tenant's rows.

Fail-closed rule: if no tenant is bound to the context, we return ``.none()``
rather than the full table. A missing tenant therefore leaks *nothing*, which is
the safe direction to fail for a data-isolation control.
"""

from django.db import models

from .context import get_current_tenant, is_unscoped


class TenantManager(models.Manager):
    def get_queryset(self):
        qs = super().get_queryset()

        # Trusted code inside an unscoped() block sees everything, on purpose.
        if is_unscoped():
            return qs

        tenant = get_current_tenant()
        if tenant is None:
            # No tenant context => expose nothing (fail closed).
            return qs.none()

        return qs.filter(tenant=tenant)
