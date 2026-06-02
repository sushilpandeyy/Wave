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

FastAPI · PostgreSQL (SQLAlchemy async + asyncpg) · Redis · Docker Compose.
(Current code = DB layer only — see below.)

## Current state — Part 1 (data) + Part 2 (load balancer)

Layout (`app/`):
- `db.py`, `models.py` — Part 1 data layer (4 models, enums, indexes). `scripts/init_db.py`
  creates tables; `scripts/seed.py` seeds one user per tier.
- `config.py` — env settings; budget is one dial: `W_MAX` (max total concurrent workers).
- `tiers.py` — `TierPolicy`, lane/priority maps, and `degrade(tier, pressure)`.
- `redis.py` — shared async client. `balancer.py` — the LB: atomic admit+enqueue Lua,
  `BZPOPMIN` dequeue, snapshot. `streaming.py` — pub/sub token bus.
- `health.py` — heartbeats/in-flight/latency. `pool.py` — `Autoscaler` + `WorkerSupervisor`.
- `worker.py` — pull loop + container entrypoint (`python -m app.worker`).
  `manager.py` — autoscaler entrypoint (`python -m app.manager`).
- `queries.py` — reusable Part-1 queries. `prompt.py`, `llm.py` (mock).
- `ratelimit.py` — per-user tier-aware token bucket (Lua). `safety.py` — local categorized
  screener (crisis/jailbreak/nsfw/boundary). `voice.py` — Wave's in-character message pools +
  `NoticeGate` (anti-spam cooldown). [Part 3]
- `api.py` — FastAPI: `WS /ws/chat` (rate-limit → safety → admit), `/healthz`, `/metrics`.

Docs: `docs/data-model.md` (Part 1), `docs/load-balancer.md` (Part 2),
`docs/safety-rate-limiting.md` (Part 3).

## Conventions

- Keep all I/O async; hot path = 1 Redis op to enqueue + 1 `BZPOPMIN`. No unnecessary awaits.
- `tier` is a first-class column on `users` (and denormalized onto `messages`), never JSON.
- A **session** is one conversation episode; ≤1 active session per user (partial unique index).
- Tier behaviour differs only by priority, reserved capacity, and degradation order — driven
  by ONE global pressure signal (folds system load + latency + a health circuit breaker; no
  per-tier latency SLAs). Three pools: priority (ent-only) / standard / elastic overflow.
  Enterprise never degrades/sheds.

## Run locally

Postgres + Redis with the default DSNs. With Docker:

```bash
docker compose up -d --build --scale worker=3
docker compose exec api python -m scripts.init_db
docker compose exec api python -m scripts.seed
```

Without Docker, run the three roles in separate shells against local servers:
`uvicorn app.api:app` · `python -m app.manager` · `python -m app.worker`.
