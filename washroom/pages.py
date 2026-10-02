from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

REFRESH_SECONDS = 15


@staff_member_required
def dashboard(request):
    """Washroom dashboard for our team; data comes from /api/v1/dashboard/ using the Admin login session."""
    return render(request, "washroom/dashboard.html", {"refresh_seconds": REFRESH_SECONDS})
