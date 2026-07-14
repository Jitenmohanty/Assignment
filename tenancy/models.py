"""Section 3 models.

``Order`` here is the scaffold model from the task (a tenant-scoped table). It is
intentionally separate from the ``orders.Order`` used in Section 1 — different
app, different table — so each section stays self-contained.
"""

from django.db import models

from .context import get_current_tenant
from .managers import TenantManager


class Tenant(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)  # also used as the subdomain label

    def __str__(self):
        return self.slug


class Order(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    description = models.CharField(max_length=255, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # Default manager: tenant-scoped. Declared first, so it is also the
    # `_default_manager` used across the app.
    objects = TenantManager()
    # Explicit escape hatch for trusted cross-tenant / system code.
    all_tenants = models.Manager()

    class Meta:
        # Django internals (cascade deletes, refresh_from_db, related lookups)
        # use the *base* manager. Point it at the unscoped manager so framework
        # machinery is never silently filtered — only application queries are.
        base_manager_name = "all_tenants"

    def save(self, *args, **kwargs):
        # Defence in depth for writes: if the row has no tenant yet, stamp it
        # from context so a developer cannot accidentally create cross-tenant
        # rows either. Reads are already covered by TenantManager.
        if self.tenant_id is None:
            current = get_current_tenant()
            if current is not None:
                self.tenant = current
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.pk} (tenant={self.tenant_id})"
