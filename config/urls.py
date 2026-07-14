from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Section 1 — orders summary endpoint (broken + fixed variants).
    path("api/orders/", include("orders.urls")),
    # Section 3 — tenant-scoped demo endpoint.
    path("api/tenancy/", include("tenancy.urls")),
    # django-silk UI: browse recorded requests and their SQL query counts.
    path("silk/", include("silk.urls", namespace="silk")),
]
