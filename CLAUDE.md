# Wave — AI Companion Message Processing Pipeline

## What we're building

Wave is an AI companion chatbot used by millions on our own app. This repo is the
**backend message-processing pipeline**: data stores, rate limiting, routing,
worker pools, safety handling, and analytics.

The backend must support subscription tiers (`free`, `premium`, `premium++`),
deliver reliable performance for higher tiers, and **degrade gracefully** for
lower tiers — without ever sounding technical or robotic to the end user.

## Technical constraints (assignment requirements)

- **Backend:** Python **FastAPI** (the spec allowed FastAPI *or* Node.js; we chose FastAPI).
- **Database:** **PostgreSQL**.
- **Distributed state:** **Redis**.
- **Deployment:** server-based, **Docker Compose** simulating multi-node.
- **Load balancing:** custom **tier-aware load balancer** — load balancer backed by
  Redis using bucket algorithms.
- **Concurrency:** all I/O **must be non-blocking** (`asyncio`). No blocking calls
  in request/worker paths.
- **Cloud target:** GCP / AWS. We optimize for **GCP** specifics.

## Stack

FastAPI · PostgreSQL (SQLAlchemy async + asyncpg) · Redis · structlog · Docker Compose.

## Layout

- `app/core/` — config, logging, and `tiers.py` (the single `POLICIES` table where
  all per-tier behavior lives: rate limits, queue lane, priority, context budget,
  timeout, model quality).
- `app/services/` — `rate_limit` (Redis token bucket), `router` (per-lane Redis
  sorted set), `chat`, `prompt`, `llm` (`MockCompletionClient` today), `safety`,
  `streaming` (Redis pub/sub), `analytics`, `registry`.
- `app/workers/` — in-process asyncio worker pool + `WorkerManager` (autoscale,
  heartbeat, idle retirement).
- `app/db/` — `postgres.py`, `redis.py`.
- `app/models/` + `app/schemas/` — SQLAlchemy models and Pydantic schemas.
- `app/api/chat.py` — WebSocket chat endpoint.
- `scripts/seed.py` — seeds one demo user per tier.

See `README.md` for the architecture diagram and run instructions.

## Conventions

- Keep all I/O async — no blocking calls in request or worker paths.
- All tier-specific behavior belongs in `app/core/tiers.py::POLICIES`, not scattered
  through services.
- User-facing safety/rate-limit messages must be warm and non-technical (see
  `app/services/safety.py` fallbacks).
- Config knobs are env-overridable with defaults in `app/core/config.py`.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose up -d            # postgres + redis
.venv/bin/python -m scripts.seed
.venv/bin/uvicorn app.main:app --reload
```

Health: `GET /healthz` · Metrics: `GET /metrics` · Chat: `ws://127.0.0.1:8000/ws/chat?user_id=<id>&session=demo`
