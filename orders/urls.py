from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    # The endpoint from the scenario. Points at the fixed implementation.
    path("summary/", views.FixedOrderSummaryView.as_view(), name="summary"),
    # Kept for side-by-side profiling / evidence in django-silk.
    path("summary/broken/", views.BrokenOrderSummaryView.as_view(),
         name="summary-broken"),
]
