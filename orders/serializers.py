"""Serializers for the order summary endpoint.

Two serializers with identical output shape, differing only in *where* the
per-order aggregates come from:

  * ``BrokenOrderSummarySerializer`` derives them in Python by walking the
    ``items`` relation and the ``customer`` FK for every order. When the view's
    queryset has not prefetched those relations, each accessor fires its own
    SQL query — the N+1 in Section 1.

  * ``FixedOrderSummarySerializer`` reads aggregates the queryset already
    computed in the database via ``.annotate()`` and a joined ``customer``.
"""

from rest_framework import serializers

from .models import Order


class BrokenOrderSummarySerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer_name",
                  "item_count", "total_amount"]

    # Each of these touches a related object. With an unoptimized queryset the
    # related rows are not in memory, so the ORM issues a fresh query per order.
    def get_customer_name(self, obj):
        return obj.customer.name                      # 1 SELECT on customer / order

    def get_item_count(self, obj):
        return obj.items.count()                      # 1 COUNT(*) / order

    def get_total_amount(self, obj):
        # Materialises every OrderItem row for this order -> 1 SELECT / order.
        return sum(i.quantity * i.unit_price for i in obj.items.all())


class FixedOrderSummarySerializer(serializers.ModelSerializer):
    # customer is select_related()'d, so this dotted access hits no extra query.
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    # These come from .annotate() on the queryset — computed in one aggregate
    # query, not per row.
    item_count = serializers.IntegerField(read_only=True)
    total_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer_name",
                  "item_count", "total_amount"]
