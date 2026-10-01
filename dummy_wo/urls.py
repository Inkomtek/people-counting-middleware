from django.urls import path

from . import views

urlpatterns = [
    path("api_iot.php", views.api_iot, name="dummy-api-iot"),
]
