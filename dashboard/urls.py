from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("people-counting/", views.people_counting, name="people_counting"),
    path("people-counting/rekap.<str:fmt>", views.export_recap, name="export_recap"),
]
