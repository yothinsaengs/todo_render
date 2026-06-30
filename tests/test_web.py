import unittest

from fastapi.testclient import TestClient

from app.auth import create_password_hash
from app.config import Settings
from app.google_store import StoreError
from app.web import create_app


class FakeStore:
    def dispatch(self, action, payload):
        if action == "update":
            raise StoreError("simulated sync failure")
        return {"action": action, "payload": payload}


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = Settings(
            username="owner",
            password_hash=create_password_hash("correct horse battery staple"),
            session_secret="test-secret-that-is-long-enough-for-tests",
            spreadsheet_id="sheet",
            google_service_account_info=None,
            google_service_account_file=None,
            cookie_secure=False,
            session_hours=1,
            activity_log_enabled=False,
        )
        cls.client = TestClient(create_app(settings, FakeStore()))

    def setUp(self):
        self.client.cookies.clear()

    def test_ping_is_public(self):
        response = self.client.get("/ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_favicon_is_public(self):
        response = self.client.get("/favicon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/svg+xml")

    def test_dashboard_requires_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_login_api_and_logout(self):
        bad = self.client.post(
            "/auth/login", json={"username": "owner", "password": "wrong"}
        )
        self.assertEqual(bad.status_code, 401)

        login = self.client.post(
            "/auth/login",
            json={"username": "owner", "password": "correct horse battery staple"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["ok"])

        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Focus board", dashboard.text)

        api = self.client.post("/api", json={"action": "meta", "payload": {"x": 1}})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json()["data"], {"action": "meta", "payload": {"x": 1}})

        queued = self.client.post(
            "/api", json={"action": "create", "payload": {"title": "Background"}}
        )
        self.assertEqual(queued.status_code, 202)
        job_id = queued.json()["data"]["jobId"]
        job = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "succeeded")
        self.assertEqual(
            job.json()["data"]["result"],
            {"action": "create", "payload": {"title": "Background"}},
        )

        removed = self.client.post(
            "/api", json={"action": "remove", "payload": {"id": "task"}}
        )
        self.assertEqual(removed.status_code, 202)
        removed_job = self.client.get(f"/api/jobs/{removed.json()['data']['jobId']}")
        self.assertEqual(removed_job.json()["data"]["status"], "succeeded")

        for action in ("merge", "undoMerge", "bulkUpdateStatus"):
            queued_write = self.client.post(
                "/api", json={"action": action, "payload": {"test": True}}
            )
            self.assertEqual(queued_write.status_code, 202)
            queued_job = self.client.get(
                f"/api/jobs/{queued_write.json()['data']['jobId']}"
            )
            self.assertEqual(queued_job.json()["data"]["status"], "succeeded")

        failed = self.client.post(
            "/api", json={"action": "update", "payload": {"id": "task"}}
        )
        failed_job = self.client.get(f"/api/jobs/{failed.json()['data']['jobId']}")
        self.assertEqual(failed_job.json()["data"]["status"], "failed")
        self.assertEqual(failed_job.json()["data"]["error"], "simulated sync failure")

        logout = self.client.post("/auth/logout")
        self.assertEqual(logout.status_code, 200)
        unauthorized = self.client.post("/api", json={"action": "meta", "payload": {}})
        self.assertEqual(unauthorized.status_code, 401)
        unauthorized_job = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(unauthorized_job.status_code, 401)


if __name__ == "__main__":
    unittest.main()
