"""Tiny endpoints used to demonstrate/verify the tenant middleware end to end."""

from django.http import JsonResponse

from .models import Order


def whoami(request):
    """Echo the tenant resolved by the middleware and that tenant's order count.

    The Order.objects.count() below has NO explicit tenant filter, yet it only
    ever counts the current tenant's rows — that is the whole point.
    """
    tenant = getattr(request, "tenant", None)
    return JsonResponse({
        "tenant": tenant.slug if tenant else None,
        "visible_orders": Order.objects.count(),  # auto-scoped
    })
