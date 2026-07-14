"""Section 1 data model.

A deliberately ordinary e-commerce shape: a Customer has many Orders, and an
Order has many OrderItems. The N+1 regression in Section 1 is triggered by
walking these relations one row at a time in the serializer.
"""

from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)

    def __str__(self):
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        SHIPPED = "shipped", "Shipped"
        CANCELLED = "cancelled", "Cancelled"

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="orders"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Newest first — matches how the mobile dashboard lists them.
        ordering = ["-created_at"]
        indexes = [
            # Composite index so "this customer's orders, newest first" is an
            # index range scan rather than a filter-then-sort.
            models.Index(fields=["customer", "-created_at"]),
        ]

    def __str__(self):
        return f"Order #{self.pk} ({self.status})"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items"
    )
    product_name = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.product_name}"
