from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from .models import ApiClient, hash_key

HEADER = "X-API-Key"


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Authenticates an external system by its X-API-Key header. request.user is the ApiClient."""

    def authenticate(self, request):
        raw_key = request.headers.get(HEADER)
        if not raw_key:
            return None
        client = ApiClient.objects.filter(key_hash=hash_key(raw_key), is_active=True).first()
        if client is None:
            raise exceptions.AuthenticationFailed("API key tidak valid")
        ApiClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
        return client, client

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when the key is missing.
        return HEADER


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = ApiKeyAuthentication
    name = "ApiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "header", "name": HEADER}

