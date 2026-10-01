from django.test import TestCase, override_settings

from .models import WorkOrder

BODY = {
    "token": "dummy-token",
    "INSTANCE": "prod",
    "LOC_ID": "GRAHA ISS BINTARO",
    "ASSET_ID": "SPACE-GRAHAISS-0020",
    "REQ_TYP": "CHECK-ROOM-TOILET",
    "REQ_DESC": "Toilet Traffic Counter",
}


@override_settings(DUMMY_WO_TOKEN="dummy-token")
class DummyApiTests(TestCase):
    url = "/dummy/api_iot.php"

    def test_valid_request_creates_work_order(self):
        response = self.client.post(self.url, BODY, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertTrue(response.json()["wo_id"].startswith("WO-"))
        self.assertTrue(WorkOrder.objects.get().accepted)

    def test_wrong_token_is_rejected(self):
        response = self.client.post(self.url, {**BODY, "token": "x"}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"status": "error", "message": "Token tidak valid"})

    def test_missing_field_is_rejected(self):
        response = self.client.post(self.url, {**BODY, "LOC_ID": ""}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
