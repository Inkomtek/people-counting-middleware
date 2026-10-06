from django.db import models


class WorkOrder(models.Model):
    """A request received by the dummy Algospection Work Order API."""

    received_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField()
    accepted = models.BooleanField(default=False)
    response_status = models.IntegerField()
    message = models.CharField(max_length=255)

    @property
    def wo_id(self):
        # Numeric like Algospection's WO_NO (e.g. "280268").
        return f"{self.pk:06d}" if self.accepted else ""

    def __str__(self):
        return self.wo_id or f"Rejected #{self.pk}"
