"""Seed one customer with many orders so the N+1 is observable in django-silk.

Usage:
    python manage.py seed_orders --orders 250 --items 3
Then hit:
    /api/orders/summary/broken/?customer_id=<id>   (many queries)
    /api/orders/summary/?customer_id=<id>          (a couple of queries)
"""

import random

from django.core.management.base import BaseCommand
from django.db import transaction

from orders.models import Customer, Order, OrderItem

PRODUCTS = ["Notebook", "Pen Set", "Desk Lamp", "Mug", "Backpack", "Headphones"]


class Command(BaseCommand):
    help = "Create a customer with a large number of orders and items."

    def add_arguments(self, parser):
        parser.add_argument("--orders", type=int, default=250)
        parser.add_argument("--items", type=int, default=3,
                            help="items per order")

    @transaction.atomic
    def handle(self, *args, **options):
        n_orders = options["orders"]
        n_items = options["items"]

        customer = Customer.objects.create(
            name="Dashboard Power User",
            email=f"power.user.{Customer.objects.count()}@example.com",
        )

        orders = Order.objects.bulk_create(
            [Order(customer=customer, status=Order.Status.PAID)
             for _ in range(n_orders)]
        )

        items = []
        for order in orders:
            for _ in range(n_items):
                items.append(OrderItem(
                    order=order,
                    product_name=random.choice(PRODUCTS),
                    quantity=random.randint(1, 5),
                    unit_price=random.choice(["9.99", "19.50", "49.00", "5.25"]),
                ))
        OrderItem.objects.bulk_create(items)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded customer id={customer.id} with {n_orders} orders "
            f"({len(items)} items).\n"
            f"Broken: /api/orders/summary/broken/?customer_id={customer.id}\n"
            f"Fixed:  /api/orders/summary/?customer_id={customer.id}"
        ))
