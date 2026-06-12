"""
ShieldClaw-MCP Control Plane.

Receives audit events streamed by `shieldclaw-mcp` proxies in the field
(POST /api/events) and serves them to the dashboard (GET /api/events,
GET /api/stats). Deliberately tiny: SQLite, JSON in/out, CORS open for
the shieldclaw.xyz dashboard.

DB path: CONTROL_DB_PATH (point at a Railway volume for durability),
defaults to ./control.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CORS(app, resources={r"/api/*": {"origins": "*"}})

DB_PATH = os.environ.get("CONTROL_DB_PATH", "control.db")
VALID_DECISIONS = {"allow", "deny", "ask"}


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts TEXT NOT NULL,
                   agent TEXT,
                   tool TEXT,
                   decision TEXT,
                   args_fingerprint TEXT,
                   received_at TEXT NOT NULL
               )"""
        )
        # Pending approvals for 'ask' tool calls: a proxy creates one and
        # polls it; a human resolves it to approved/denied.
        c.execute(
            """CREATE TABLE IF NOT EXISTS approvals (
                   id TEXT PRIMARY KEY,
                   created_at TEXT NOT NULL,
                   agent TEXT,
                   tool TEXT,
                   args_fingerprint TEXT,
                   status TEXT NOT NULL DEFAULT 'pending',
                   decided_at TEXT,
                   decided_by TEXT
               )"""
        )


init_db()


@app.get("/health")
def health():
    return jsonify({"service": "ShieldClaw-MCP Control Plane", "status": "healthy"})


@app.post("/api/events")
def ingest():
    e = request.get_json(silent=True) or {}
    decision = e.get("decision")
    if decision not in VALID_DECISIONS:
        return jsonify({"error": "decision must be allow|deny|ask"}), 400
    with _conn() as c:
        c.execute(
            "INSERT INTO events (ts, agent, tool, decision, args_fingerprint, received_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                e.get("ts") or datetime.now(timezone.utc).isoformat(),
                str(e.get("agent", "unknown"))[:120],
                str(e.get("tool", "unknown"))[:120],
                decision,
                str(e.get("args_fingerprint", ""))[:64],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return jsonify({"ok": True}), 201


@app.get("/api/events")
def list_events():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except ValueError:
        limit = 50
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, agent, tool, decision, args_fingerprint FROM events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify({"events": [dict(r) for r in rows]})


@app.get("/api/stats")
def stats():
    with _conn() as c:
        rows = c.execute("SELECT decision, COUNT(*) n FROM events GROUP BY decision").fetchall()
        total = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    by = {r["decision"]: r["n"] for r in rows}
    return jsonify({
        "total": total,
        "allowed": by.get("allow", 0),
        "blocked": by.get("deny", 0),
        "pending": by.get("ask", 0),
    })


# ── Approvals (human-in-the-loop for 'ask' tool calls) ──────────────────────

APPROVAL_STATES = {"approved", "denied"}


@app.post("/api/approvals")
def create_approval():
    """A proxy registers a pending approval and gets back an id to poll."""
    e = request.get_json(silent=True) or {}
    aid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            "INSERT INTO approvals (id, created_at, agent, tool, args_fingerprint, status) "
            "VALUES (?,?,?,?,?,'pending')",
            (
                aid,
                datetime.now(timezone.utc).isoformat(),
                str(e.get("agent", "unknown"))[:120],
                str(e.get("tool", "unknown"))[:120],
                str(e.get("args_fingerprint", ""))[:64],
            ),
        )
    return jsonify({"id": aid, "status": "pending"}), 201


@app.get("/api/approvals/<aid>")
def get_approval(aid):
    """Poll a single approval's status."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, agent, tool, args_fingerprint, status, created_at, decided_at, decided_by "
            "FROM approvals WHERE id = ?",
            (aid,),
        ).fetchone()
    if not row:
        return jsonify({"error": "approval not found"}), 404
    return jsonify(dict(row))


@app.post("/api/approvals/<aid>/decision")
def decide_approval(aid):
    """A human approves or denies a pending approval."""
    e = request.get_json(silent=True) or {}
    decision = e.get("decision")
    if decision not in APPROVAL_STATES:
        return jsonify({"error": "decision must be approved|denied"}), 400
    with _conn() as c:
        row = c.execute("SELECT status FROM approvals WHERE id = ?", (aid,)).fetchone()
        if not row:
            return jsonify({"error": "approval not found"}), 404
        if row["status"] != "pending":
            # Already decided — idempotent, report the existing state.
            return jsonify({"id": aid, "status": row["status"], "already_decided": True})
        c.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=?",
            (decision, datetime.now(timezone.utc).isoformat(),
             str(e.get("by", "dashboard"))[:120], aid),
        )
    return jsonify({"id": aid, "status": decision})


@app.get("/api/approvals")
def list_approvals():
    """List approvals, optionally filtered by status (default: pending)."""
    status = request.args.get("status", "pending")
    with _conn() as c:
        if status == "all":
            rows = c.execute(
                "SELECT id, agent, tool, args_fingerprint, status, created_at, decided_at, decided_by "
                "FROM approvals ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, agent, tool, args_fingerprint, status, created_at, decided_at, decided_by "
                "FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT 100",
                (status,),
            ).fetchall()
    return jsonify({"approvals": [dict(r) for r in rows]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8095)))
