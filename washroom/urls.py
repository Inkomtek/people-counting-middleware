from django.urls import path

from . import views

urlpatterns = [
    path("readings/", views.ReadingListCreateView.as_view(), name="washroom-readings"),
    path("customer-responses/", views.CustomerResponseListCreateView.as_view(), name="washroom-customer-responses"),
]
