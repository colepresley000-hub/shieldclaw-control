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


# ── approvals ────────────────────────────────────────────────────────────────

def test_approval_create_poll_decide(client):
    # create
    r = client.post("/api/approvals", json={"agent": "claude", "tool": "send_email",
                                            "args_fingerprint": "abc"})
    assert r.status_code == 201
    aid = r.get_json()["id"]
    assert r.get_json()["status"] == "pending"

    # poll -> still pending
    assert client.get(f"/api/approvals/{aid}").get_json()["status"] == "pending"

    # appears in pending list
    pend = client.get("/api/approvals?status=pending").get_json()["approvals"]
    assert any(a["id"] == aid for a in pend)

    # decide -> approved
    d = client.post(f"/api/approvals/{aid}/decision", json={"decision": "approved", "by": "cole"})
    assert d.status_code == 200 and d.get_json()["status"] == "approved"

    # poll now reflects approved + decided_by
    got = client.get(f"/api/approvals/{aid}").get_json()
    assert got["status"] == "approved" and got["decided_by"] == "cole"

    # no longer pending
    pend2 = client.get("/api/approvals?status=pending").get_json()["approvals"]
    assert all(a["id"] != aid for a in pend2)


def test_approval_decision_validation_and_idempotency(client):
    aid = client.post("/api/approvals", json={"tool": "db_write"}).get_json()["id"]
    assert client.post(f"/api/approvals/{aid}/decision", json={"decision": "maybe"}).status_code == 400
    client.post(f"/api/approvals/{aid}/decision", json={"decision": "denied"})
    # deciding again is idempotent, keeps first decision
    again = client.post(f"/api/approvals/{aid}/decision", json={"decision": "approved"}).get_json()
    assert again["status"] == "denied" and again.get("already_decided")


def test_approval_404(client):
    assert client.get("/api/approvals/nope").status_code == 404
    assert client.post("/api/approvals/nope/decision", json={"decision": "approved"}).status_code == 404
