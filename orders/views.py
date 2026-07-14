"""Section 1 — the broken endpoint and its fix.

The mobile dashboard calls ``/api/orders/summary/?customer_id=<id>``. Both the
broken and fixed implementations are exposed side by side so django-silk records
two comparable requests and the query-count difference is visible in its UI.
"""

from django.db.models import Count, DecimalField, F, Sum
from django.db.models.functions import Coalesce
from rest_framework.generics import ListAPIView

from .models import Order
from .serializers import BrokenOrderSummarySerializer, FixedOrderSummarySerializer


class _BaseOrderSummaryView(ListAPIView):
    """Shared filtering: one customer's orders, as the mobile dashboard requests."""

    def get_customer_id(self):
        return self.request.query_params.get("customer_id")

    def base_queryset(self):
        qs = Order.objects.all()
        customer_id = self.get_customer_id()
        if customer_id:
            qs = qs.filter(customer_id=customer_id)
        return qs


class BrokenOrderSummaryView(_BaseOrderSummaryView):
    """Reproduces the regression.

    The queryset selects only Order rows. The serializer then reaches into
    ``customer`` and ``items`` for every order, so total queries grow linearly
    with the number of orders:

        1 (orders) + N (customer) + N (item count) + N (item rows) = 1 + 3N

    For a customer with 200 orders that is ~601 round-trips — the 30s timeout.
    """

    serializer_class = BrokenOrderSummarySerializer

    def get_queryset(self):
        return self.base_queryset()


class FixedOrderSummaryView(_BaseOrderSummaryView):
    """The fix: push the per-order work into the database.

    * ``select_related("customer")`` turns the customer lookup into an SQL JOIN
      resolved in the single orders query (customer is a to-one FK).
    * ``.annotate(...)`` computes the item count and revenue with GROUP BY
      aggregates in the same query, so no OrderItem rows are shipped to Python
      and there is no per-order query.

    Total queries: a small constant (~1) regardless of order count.
    """

    serializer_class = FixedOrderSummarySerializer

    def get_queryset(self):
        money = DecimalField(max_digits=12, decimal_places=2)
        return (
            self.base_queryset()
            .select_related("customer")
            .annotate(
                # The join on `items` fans out one row per item; distinct=True
                # keeps the count correct, while Sum aggregates over those same
                # joined rows for revenue — both in one GROUP BY, no item rows
                # shipped to Python.
                item_count=Count("items", distinct=True),
                total_amount=Coalesce(
                    Sum(F("items__quantity") * F("items__unit_price")),
                    0,
                    output_field=money,
                ),
            )
        )
