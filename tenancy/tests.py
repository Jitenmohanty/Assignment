"""Section 3 tests — prove tenant isolation cannot be bypassed.

These assert the *negative*: that tenant A's queries cannot reach tenant B's
rows through any ordinary ORM call, and that ``.objects.all()`` is scoped rather
than a bypass.
"""

import asyncio

from django.test import TestCase

from .context import (get_current_tenant, set_current_tenant,
                      clear_current_tenant, unscoped)
from .models import Order, Tenant
from .tokens import make_tenant_token


class TenantScopingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Acme", slug="acme")
        cls.tenant_b = Tenant.objects.create(name="Globex", slug="globex")
        # Create rows for each tenant using the unscoped manager (system setup).
        Order.all_tenants.create(tenant=cls.tenant_a, description="A-1")
        Order.all_tenants.create(tenant=cls.tenant_a, description="A-2")
        cls.b_order = Order.all_tenants.create(tenant=cls.tenant_b, description="B-1")

    def tearDown(self):
        clear_current_tenant()

    def test_all_is_scoped_to_current_tenant(self):
        set_current_tenant(self.tenant_a)
        self.assertEqual(Order.objects.count(), 2)
        self.assertTrue(
            all(o.tenant_id == self.tenant_a.id for o in Order.objects.all())
        )

    def test_cannot_read_other_tenants_row_by_pk(self):
        set_current_tenant(self.tenant_a)
        # Tenant B's order exists, but is invisible to tenant A via .get().
        with self.assertRaises(Order.DoesNotExist):
            Order.objects.get(pk=self.b_order.pk)
        # ...and via .filter() targeting B's tenant explicitly.
        self.assertFalse(Order.objects.filter(tenant=self.tenant_b).exists())

    def test_switching_tenant_switches_visible_rows(self):
        set_current_tenant(self.tenant_a)
        self.assertEqual(Order.objects.count(), 2)
        set_current_tenant(self.tenant_b)
        self.assertEqual(Order.objects.count(), 1)

    def test_no_tenant_context_fails_closed(self):
        # No tenant bound => zero rows, never the whole table.
        self.assertIsNone(get_current_tenant())
        self.assertEqual(Order.objects.count(), 0)

    def test_unscoped_is_explicit_optin(self):
        set_current_tenant(self.tenant_a)
        with unscoped():
            self.assertEqual(Order.objects.count(), 3)  # all tenants
        # Back to scoped after the block.
        self.assertEqual(Order.objects.count(), 2)

    def test_save_autostamps_tenant(self):
        set_current_tenant(self.tenant_b)
        o = Order(description="no explicit tenant")
        o.save()  # tenant filled from context
        self.assertEqual(o.tenant_id, self.tenant_b.id)


class TenantMiddlewareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Acme", slug="acme")
        cls.tenant_b = Tenant.objects.create(name="Globex", slug="globex")
        Order.all_tenants.create(tenant=cls.tenant_a, description="A-1")
        Order.all_tenants.create(tenant=cls.tenant_a, description="A-2")
        Order.all_tenants.create(tenant=cls.tenant_b, description="B-1")

    def test_jwt_header_binds_tenant(self):
        token = make_tenant_token("acme")
        resp = self.client.get(
            "/api/tenancy/whoami/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(resp.json(), {"tenant": "acme", "visible_orders": 2})

    def test_subdomain_binds_tenant(self):
        resp = self.client.get(
            "/api/tenancy/whoami/", HTTP_HOST="globex.artikate.test"
        )
        self.assertEqual(resp.json(), {"tenant": "globex", "visible_orders": 1})

    def test_context_cleared_after_request(self):
        token = make_tenant_token("acme")
        self.client.get("/api/tenancy/whoami/", HTTP_AUTHORIZATION=f"Bearer {token}")
        # Middleware's finally-block must have reset the context.
        self.assertIsNone(get_current_tenant())

    def test_unknown_tenant_fails_closed(self):
        resp = self.client.get(
            "/api/tenancy/whoami/", HTTP_HOST="nope.artikate.test"
        )
        self.assertEqual(resp.json(), {"tenant": None, "visible_orders": 0})

    def test_expired_jwt_is_rejected(self):
        # A token that expired 10s ago must not bind a tenant (fail closed).
        expired = make_tenant_token("acme", ttl_seconds=-10)
        resp = self.client.get(
            "/api/tenancy/whoami/", HTTP_AUTHORIZATION=f"Bearer {expired}"
        )
        self.assertEqual(resp.json(), {"tenant": None, "visible_orders": 0})


class TenantAsyncContextTests(TestCase):
    """Proves the contextvars choice: concurrent async tasks don't leak tenants.

    Each asyncio Task runs in its own copied context, so a ContextVar set inside
    one task is invisible to the other even though both run on the same
    event-loop thread. A ``threading.local`` would FAIL this test because both
    coroutines would share the one thread-local slot.

    This is DB-free on purpose: it isolates the context mechanism, which is the
    thing under test.
    """

    def test_concurrent_tasks_do_not_leak_context(self):
        async def resolve(slug):
            set_current_tenant(slug)
            await asyncio.sleep(0)  # let the other task interleave
            tenant = get_current_tenant()
            await asyncio.sleep(0)
            return tenant

        async def run():
            return await asyncio.gather(resolve("acme"), resolve("globex"))

        results = asyncio.run(run())
        self.assertEqual(results, ["acme", "globex"])
