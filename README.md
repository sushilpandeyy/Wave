# Wave — Message Processing Pipeline

FastAPI + PostgreSQL + Redis backend for the Wave AI companion. Handles tiered
chat completion: rate limiting, priority routing, an autoscaling worker pool,
safety handling, and live analytics — with graceful degradation for lower tiers.

## Architecture

```
WS client ──► /ws/chat ──► ChatService ──► [rate limit] ──► [safety in]
                 ▲                              │
                 │ stream tokens                ▼
          Redis pub/sub                 persist user Chat row
        wave:stream:{msg_id}                    │
                 ▲                               ▼
                 │                     enqueue → Redis ZSET lane (high|low)
                 │                               │
                 │              ┌────────────────┴──────────── pull (BZPOPMIN)
          WorkerManager ──► [ Worker pool: asyncio tasks ]
        (autoscale + heartbeat)         build prompt → mock LLM stream
                                        → safety out → persist assistant Chat
```

- **Tiers** (`app/core/tiers.py`): `free`, `premium`, `premium++`. All
  per-tier behavior (rate limits, queue lane, priority, context budget, timeout,
  model quality) lives in one `POLICIES` table. Tier is read from
  `users.profile["tier"]`.
- **Rate limiting** (`app/services/rate_limit.py`): atomic Redis token bucket.
- **Routing** (`app/services/router.py`): per-lane Redis sorted set; premium
  tiers ride the HIGH lane, free rides LOW. Workers pull HIGH-first.
- **Workers** (`app/workers/`): in-process asyncio pool. `WorkerManager`
  autoscales between `min_workers` and `max_workers` on queue depth / job age,
  retires idle workers, and prunes dead heartbeats.
- **Streaming** (`app/services/streaming.py`): worker → client token streaming
  over Redis pub/sub, forwarded to the WebSocket.
- **Safety** (`app/services/safety.py`): pluggable input/output screening with
  warm, non-technical fallbacks.
- **Analytics** (`app/services/analytics.py`): per-minute Redis counters.
- **LLM** (`app/services/llm.py`): `MockCompletionClient` for now; swap in a real
  provider by implementing `CompletionClient.stream`.

## Run locally

```bash
# 1. Dependencies
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Data stores
docker compose up -d            # postgres + redis

# 3. Seed demo users (one per tier)
.venv/bin/python -m scripts.seed

# 4. Start the API (tables auto-create on startup in dev)
.venv/bin/uvicorn app.main:app --reload
```

Health: `GET /healthz` · Live metrics: `GET /metrics`

### Talk to it (WebSocket)

Connect to `ws://127.0.0.1:8000/ws/chat?user_id=<id>&session=demo` and send
`{"message": "..."}`. You'll receive a stream of `token` frames, then a `done`
frame — or a `notice` frame when rate-limited or safety-blocked.

## Configuration

All knobs are env-overridable; defaults live in `app/core/config.py`
(worker counts, autoscale thresholds, heartbeat TTL, etc.). See `.env.example`.

## Notes / out of scope (next steps)

- Real LLM provider (mock today; one class to swap).
- AuthN/AuthZ (a trusted `user_id` is assumed for now).
- Alembic migrations (dev uses `create_all`).
- Separate worker processes / k8s HPA (the in-process pool simulates scaling).
