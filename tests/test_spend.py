import os
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

folder = tempfile.TemporaryDirectory()
os.environ.update(SPENDLENS_DATA=folder.name, SPENDLENS_DEMO="1", SPENDLENS_TOKEN="")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("PGHOST", None)

from backend.main import app, engine


def tearDownModule():
    engine.dispose()
    folder.cleanup()


class SpendTest(unittest.TestCase):
    def test_seeded_spike_and_replay(self):
        with TestClient(app) as client:
            baseline = client.get("/api/dashboard?tenant=northstar").json()
            peak = max(baseline["timeline"], key=lambda row: row["cost_nanos"])
            detail = client.get("/api/dashboard", params={"tenant": "northstar", "day": peak["day"]}).json()
            self.assertEqual(len(detail["events"]), 3)
            self.assertTrue(all(row["model"] == "atlas-pro" and row["feature"] == "document-summary" for row in detail["events"]))
            self.assertEqual(sum(row["cost_nanos"] for row in detail["events"]), peak["cost_nanos"])
            usage = {key: detail["events"][0][key] for key in
                     ("tenant", "request_id", "model", "feature", "at", "input_tokens", "output_tokens")}
            usage["at"] = datetime.fromisoformat(usage["at"]).replace(tzinfo=timezone.utc).isoformat()
            self.assertEqual(client.post("/api/usage", json=usage).status_code, 200)
            self.assertEqual(client.post("/api/usage", json={**usage, "input_tokens": 1}).status_code, 409)
            after = client.get("/api/dashboard?tenant=northstar").json()
            self.assertEqual(after["total_nanos"], baseline["total_nanos"])
            self.assertEqual(after["alerts"], baseline["alerts"])
            self.assertEqual(sorted(alert["threshold"] for alert in after["alerts"]), [80, 100])

    def test_new_rate_preserves_historical_cost(self):
        with TestClient(app) as client:
            day = datetime.now(timezone.utc).date().isoformat()
            first_rate = {"model":"custom", "effective":"2024-01-01", "input_rate":1200, "output_rate":2500}
            self.assertEqual(client.post("/api/rates", json=first_rate).status_code, 201)
            usage = {"tenant":"demo-test", "request_id":"one", "model":"custom", "feature":"review",
                     "at":day + "T00:00:00Z", "input_tokens":100, "output_tokens":10}
            self.assertEqual(client.post("/api/usage", json=usage).json()["cost_nanos"], 145000)
            new_rate = {**first_rate, "effective":day, "input_rate":2200}
            self.assertEqual(client.post("/api/rates", json=new_rate).status_code, 201)
            self.assertEqual(client.post("/api/usage", json={**usage, "request_id":"two"}).json()["cost_nanos"], 245000)
            self.assertEqual(client.post("/api/usage", json=usage).json()["cost_nanos"], 145000)
