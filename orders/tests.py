"""Section 1 tests — prove the N+1 exists and the fix removes it.

The key assertion is on *query count*, not just correctness: the broken view's
query count scales with the number of orders, while the fixed view stays flat.
This is the machine-checkable version of the django-silk evidence.

The views are driven with ``APIRequestFactory`` (no middleware), so the recorded
query counts reflect only the ORM behaviour under test — not silk's own
bookkeeping writes.
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory

from .models import Customer, Order, OrderItem
from .views import BrokenOrderSummaryView, FixedOrderSummaryView

factory = APIRequestFactory()


class OrderSummaryQueryCountTests(TestCase):
    N_ORDERS = 25
    ITEMS_PER_ORDER = 3

    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(name="Acme", email="acme@example.com")
        cls._make_orders(cls.customer, cls.N_ORDERS, cls.ITEMS_PER_ORDER,
                         unit_price="10.00", quantity=2)

    @staticmethod
    def _make_orders(customer, n_orders, n_items, unit_price, quantity):
        orders = Order.objects.bulk_create(
            [Order(customer=customer, status=Order.Status.PAID)
             for _ in range(n_orders)]
        )
        OrderItem.objects.bulk_create([
            OrderItem(order=o, product_name="Widget", quantity=quantity,
                      unit_price=Decimal(unit_price))
            for o in orders for _ in range(n_items)
        ])
        return orders

    def _call(self, view_cls):
        request = factory.get("/api/orders/summary/",
                              {"customer_id": self.customer.id})
        with CaptureQueriesContext(connection) as ctx:
            response = view_cls.as_view()(request)
            response.render()  # force serialization -> triggers any N+1
        self.assertEqual(response.status_code, 200)
        return response, len(ctx.captured_queries)

    def test_broken_view_scales_with_order_count(self):
        """Broken view issues at least one query per order (N+1)."""
        _, n_queries = self._call(BrokenOrderSummaryView)
        self.assertGreater(n_queries, self.N_ORDERS)

    def test_fixed_view_uses_constant_queries(self):
        """Fixed view resolves everything in a single aggregate query."""
        _, n_queries = self._call(FixedOrderSummaryView)
        self.assertLessEqual(n_queries, 2)

    def test_both_views_return_identical_data(self):
        """The fix must not change the response payload."""
        broken, _ = self._call(BrokenOrderSummaryView)
        fixed, _ = self._call(FixedOrderSummaryView)

        def by_id(rows):
            return {r["id"]: (r["item_count"], str(r["total_amount"]),
                              r["customer_name"]) for r in rows}

        self.assertEqual(by_id(broken.data), by_id(fixed.data))

    def test_fixed_view_query_count_independent_of_scale(self):
        """Doubling the data must not increase the fixed view's query count."""
        _, before = self._call(FixedOrderSummaryView)
        self._make_orders(self.customer, self.N_ORDERS, self.ITEMS_PER_ORDER,
                          unit_price="5.00", quantity=1)
        _, after = self._call(FixedOrderSummaryView)
        self.assertEqual(before, after)
