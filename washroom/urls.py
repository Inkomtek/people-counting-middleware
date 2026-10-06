from django.urls import path, re_path

from . import views

urlpatterns = [
    path("readings/", views.ReadingListCreateView.as_view(), name="washroom-readings"),
    path("customer-responses/", views.CustomerResponseListCreateView.as_view(), name="washroom-customer-responses"),
    # Same endpoints without the trailing slash: Django cannot redirect a POST to the slash URL.
    re_path(r"^readings$", views.ReadingListCreateView.as_view()),
    re_path(r"^customer-responses$", views.CustomerResponseListCreateView.as_view()),
]
