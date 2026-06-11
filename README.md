# ShieldClaw-MCP Control Plane

Receives audit events from `shieldclaw-mcp` proxies and serves them to the
dashboard.

- `POST /api/events` — ingest one audit event `{ts,agent,tool,decision,args_fingerprint}`
- `GET /api/events?limit=N` — recent events, newest first
- `GET /api/stats` — counts: total / allowed / blocked / pending
- `GET /health`

SQLite at `CONTROL_DB_PATH` (use a Railway volume for durability). Deploy: `gunicorn app:app`.
