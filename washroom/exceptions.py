from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Wrap DRF errors (401, 404, 405, ...) in the same {"status": "error", "message": ...} shape."""
    response = exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"status": "error", "message": str(response.data["detail"])}
    return response
