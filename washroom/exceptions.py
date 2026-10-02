from rest_framework.exceptions import NotAuthenticated
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Wrap DRF errors (401, 404, 405, ...) in the same {"status": "error", "message": ...} shape."""
    response = exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "detail" in response.data:
        message = str(response.data["detail"])
        if isinstance(exc, NotAuthenticated):
            message = "API key wajib dikirim di header X-API-Key"
        response.data = {"status": "error", "message": message}
    return response
