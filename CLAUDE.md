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

## Current state — DB layer only

The repo is intentionally stripped down to **Part 1 (Data Modeling & Query Design)**.
The earlier pipeline (api/services/workers/routing) was removed and will be rebuilt
later. Only the database layer exists right now:

- `app/db.py` — async SQLAlchemy `Base`, engine, `SessionLocal`, `get_session`
  (DSN from `POSTGRES_DSN`).
- `app/models.py` — the four models (`User`, `Personality`, `Session`, `Message`)
  with the `Tier` / `MessageRole` enums and all indexes.
- `scripts/init_db.py` — `create_all` helper.

The schema, indexes, and core queries are documented in `README.md`
("Data Modeling & Query Design").

## Conventions

- Keep all I/O async — no blocking calls.
- `tier` is a first-class column on `users` (and denormalized onto `messages`),
  never buried in JSON.
- A **session** is one conversation episode; ≤1 active session per user (enforced by
  a partial unique index).

## Run locally

Needs a Postgres with a `wave` role + `wave` db (the default `POSTGRES_DSN`). A local
`.pgdata` cluster works — no Docker required:

```bash
pg_ctl -D .pgdata -l pg.log start   # (initdb -U wave -D .pgdata first time)
pip install -r requirements.txt
python -m scripts.init_db           # create tables + indexes
```

Docker is optional: `docker compose up -d` is an alternative to the local cluster.
