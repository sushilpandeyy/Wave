# Wave

**Wave** is an AI companion chatbot with subscription tiers (`free`, `premium`,
`premium++`), built on FastAPI + PostgreSQL + Redis, with OpenAI GPT for completions
(set `OPENAI_API_KEY`; without it a mock client is used so everything runs offline).

Built so far: **Part 1** (data model — schema, indexes, queries) and **Part 2** (the
tier-aware load balancer — routing, worker pools, autoscaling, and load shedding).

## Quickstart

Everything with Docker — postgres, redis, api, the autoscaler (`manager`), and a pool of
`worker` containers:

```bash
docker compose up -d --build --scale worker=3
docker compose exec api python -m scripts.init_db   # tables + indexes
docker compose exec api python -m scripts.seed      # one user per tier (prints ids)
```

Then open a WebSocket to `ws://127.0.0.1:8000/ws/chat?user_id=<id>`, send
`{"message": "..."}`, and receive `token` frames then a `done` frame. Live state is at
`GET /metrics`; health at `GET /healthz`.

> No Docker? Point `POSTGRES_DSN`/`REDIS_URL` at local servers and run the three roles in
> separate shells: `uvicorn app.api:app`, `python -m app.manager`, `python -m app.worker`.

---

## Data model

Four tables, related like this:

```
users ──1:1── personalities
  │
  └──1:N── sessions ──1:N── messages
```

| Table | What it holds | Columns |
|---|---|---|
| **users** | account + subscription tier | `id`, `display_name`, `tier`, `locale`, `timezone`, `last_active_at`, `settings` (jsonb), `created_at` |
| **personalities** | the companion's persona, one per user | `id`, `user_id` (unique), `traits` (jsonb), `summary`, `updated_at`, `created_at` |
| **sessions** | one conversation episode | `id`, `user_id`, `status` (`active`\|`closed`), `title`, `message_count`, `last_message_at`, `created_at` |
| **messages** | a single chat turn | `id`, `session_id`, `user_id`, `tier`, `role` (`user`\|`assistant`\|`system`), `content`, `mood`, `created_at` |

## Decisions we made (and why)

- **A "session" is one conversation *episode*.** A user has **at most one active
  session** at a time; a new one opens after the previous closes. Clean unit for
  scoping context and answering "what are we talking about right now."
- **`tier` is a real column, not JSON.** It's read on every message and grouped on in
  analytics — a typed column gets an index and a cheap `GROUP BY`; a JSON blob doesn't.
- **`messages` carries its own `user_id` and `tier`.** Denormalized on purpose: the hot
  reads and per-tier counts never have to join back to `sessions`/`users`. `tier` is the
  tier *at send time*, which is what those counts actually want.
- **One personality per user, updated in place.** Kept simple for now (versioning can
  come later if we want to track how a persona evolved).
- **Defaults live in the database**, not just the ORM — so plain SQL inserts work too.
- **UUID primary keys, `timestamptz` everywhere (UTC).** `mood` is nullable until a
  message is classified.

Indexes and the exact query patterns (with performance notes) live in
**[docs/data-model.md](docs/data-model.md)**.

## Tier-aware load balancer

Incoming chats are admitted and routed to **pull-based worker pools** by tier. The hot path
is one atomic Redis op to enqueue and one blocking `BZPOPMIN` to dequeue — no central
dispatcher.

- **Three pools** (`priority` enterprise-only · `standard` · elastic `overflow`) pulling
  three lanes `q:ent > q:prem > q:free` in priority order. The priority pool keeps
  `premium++` stable under any load.
- **One pressure signal** folds all four routing inputs — tier, system load, latency, and
  pool health (a circuit breaker) — into one number that drives autoscaling (within a `W_MAX`
  worker budget) and shedding.
- Under pressure, traffic degrades **lowest-tier-first**: free shrinks context/model, then
  soft-rejects at the top of the scale; premium degrades slower; enterprise never does.

How it works, the Redis key map, and the algorithms are in
**[docs/load-balancer.md](docs/load-balancer.md)**.

## Graceful rate limiting & safety

Wave never returns a harsh system message. At the front of the producer path, each message
passes a per-user **token-bucket rate limit** (one Redis Lua op) and a **safety screen**
(local, no await) before it's enqueued.

- Over the limit → she says one warm line ("okay okay, I need a tiny breather — give me a
  moment"), then goes quiet — repeated hits are silenced, never spammed. Subsequent limits =
  silence (defined behavior).
- "Approaching" the limit → one gentle "let's pace ourselves" heads-up, still served.
- Unsafe input → an in-character response, not an error. Detection is **model-driven**: the
  model emits a control flag (`jailbreak` / `nsfw` / `boundary` / `crisis`) and the app swaps in
  Wave's matching line — jailbreaks get a playful deflect, NSFW a gentle boundary, and a
  **crisis (self-harm) message gets a caring reply**. No brittle keyword regex.
- Enterprise limits are set so high it's effectively never rate-limited.
- A coarse **per-IP guard** at connection accept catches floods / rotating fake `user_id`s
  (the WebSocket is unauthenticated). Volumetric/DoS limiting in production would also sit at
  the GCP edge (Cloud Armor / the load balancer).

Details in **[docs/safety-rate-limiting.md](docs/safety-rate-limiting.md)**.

## Observability

Analytics, structured logging, and tracing that never slow the chat path — the only hot-path
cost is an in-memory enqueue.

- **Analytics** — `track()` is a `put_nowait` on a bounded queue (**~0.36 µs/call**); a
  background task batches and flushes to Redis. Under pressure it sheds *verbose* events first
  and keeps critical ones.
- **Structured logging** — JSON to stdout written off-loop (QueueHandler/Listener); a
  correlation id (the `message_id`) and context (tier, user, op) are stamped on every line.
- **Tracing** — milestones `received → enqueued → dequeued → first_token → completed →
  delivered` let you reconstruct one turn's **round-trip** and its breakdown (queue wait, TTFT,
  generation) by `corr_id`. `timed()` auto-logs any operation over a threshold.
- **Graceful shutdown** — on SIGTERM the analytics queue drains and logs flush; no data loss.

Details in **[docs/observability.md](docs/observability.md)**.
