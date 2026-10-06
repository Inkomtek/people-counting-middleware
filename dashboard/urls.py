from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("people-counting/", views.people_counting, name="people_counting"),
    path("people-counting/detail/", views.people_counting_detail, name="people_counting_detail"),
    path("people-counting/detail/<str:kind>.<str:fmt>", views.export, name="export"),
]
