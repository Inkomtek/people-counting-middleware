from django.urls import path

from . import views

urlpatterns = [
    path("api_iot.php", views.api_iot, name="dummy-api-iot"),
    path("inbox/<str:kind>/", views.inbox, name="dummy-inbox"),
    path("inbox/<str:kind>/<path:rest>", views.inbox, name="dummy-inbox-path"),
]
