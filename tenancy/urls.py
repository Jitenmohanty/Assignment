from django.urls import path

from . import views

app_name = "tenancy"

urlpatterns = [
    path("whoami/", views.whoami, name="whoami"),
]
