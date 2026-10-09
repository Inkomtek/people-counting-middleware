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


class InboxMessage(models.Model):
    """A message received by the dummy inbox, standing in for Telegram / WhatsApp (Evolution) / webhooks
    in demos and local tests. Secret headers are masked before storing."""

    received_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=50)
    path = models.CharField(max_length=300, blank=True)
    method = models.CharField(max_length=10)
    headers = models.JSONField(default=dict, blank=True)
    body = models.JSONField(null=True, blank=True)
    response_status = models.IntegerField()

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.kind} #{self.pk}"
