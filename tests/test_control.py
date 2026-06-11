import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CONTROL_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

import app as control  # noqa: E402


@pytest.fixture
def client():
    control.app.config["TESTING"] = True
    return control.app.test_client()


def test_health(client):
    assert client.get("/health").get_json()["status"] == "healthy"


def test_ingest_and_list(client):
    for d in ("allow", "deny", "ask", "deny"):
        r = client.post("/api/events", json={"agent": "a1", "tool": "t", "decision": d})
        assert r.status_code == 201
    evts = client.get("/api/events?limit=10").get_json()["events"]
    assert len(evts) == 4
    assert evts[0]["decision"] == "deny"  # newest first


def test_ingest_rejects_bad_decision(client):
    assert client.post("/api/events", json={"decision": "maybe"}).status_code == 400


def test_stats(client):
    s = client.get("/api/stats").get_json()
    assert s["total"] >= 4
    assert s["blocked"] >= 2 and s["allowed"] >= 1 and s["pending"] >= 1
