from django.db import models

# Create your models here.


class EventLog(models.Model):
    event_type = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    logged_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_type} - {self.logged_at}"
