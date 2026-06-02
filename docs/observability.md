# Observability — Analytics, Structured Logging, Tracing

Observability that never slows the chat path. The only thing on the hot path is an in-memory
enqueue; all real I/O (Redis writes, stdout writes) happens off the event loop. One correlation
id — the `message_id` — ties a turn together across the `api` and `worker` containers.

## Analytics pipeline (`app/analytics.py`)

- **`track(event, *, critical=False, **fields)`** builds a small dict and does a `put_nowait` on
  a bounded `asyncio.Queue`. No await, **~0.36 µs/call** (measured) — well under the 1 ms budget.
- **Intelligent drop under pressure:** past a soft watermark (default 80% of the queue) *verbose*
  events are dropped immediately while **critical** ones (`received`, `delivered`, `completed`,
  `error`, `timeout`, `shed`, `rate_limited`, `safety`, `crisis`) are kept until the queue is
  truly full. A `dropped` counter is exposed on `/metrics`.
- **Background flusher** batches the queue (size or a short linger) and writes in **one Redis
  pipeline**: per-minute counters `wave:metrics:{event}[:{tier}]:{minute}`, timing aggregates
  `wave:timing:{op}:{minute}` (sum+count → averages), and a capped raw-event stream
  `wave:events` (`XADD MAXLEN ~`) so a corr_id's journey is queryable without a log backend.
- **`aclose()`** stops intake, drains within a bounded time, and does a final flush.

## Structured logging (`app/obs.py`)

- **JSON to stdout, non-blocking:** a stdlib `QueueHandler` → `QueueListener` writes on a
  background thread; the event loop only enqueues.
- **Correlation + context via `contextvars`:** `set_corr(message_id)` and `bind(tier, user_id,
  op, pool…)` — a logging filter stamps `corr_id` + context onto **every** record automatically.
- **Slow-op detection:** `async with obs.timed("op", threshold_ms=...)` times a block, records a
  timing event, and auto-logs a `WARNING` when it exceeds the threshold (default `SLOW_OP_MS`).
  Wrapped around the DB reads, `admit`, and generation.

## Round-trip tracing

The `message_id` flows in the job payload, so the same `corr_id` appears on every log line for a
turn. Milestones (each a log + an analytics counter):

| stage | where | timing captured |
|---|---|---|
| `received` | api | t0 (same-process clock) |
| `enqueued` | balancer adds `enqueued_at` to the payload | — |
| `dequeued` | worker | `queue_wait_ms` = now − enqueued_at |
| `first_token` | worker | `ttft_ms` (time to first streamed token) |
| `completed` | worker | generation span via `timed("generate")` |
| `delivered` | api, when the `done` frame is sent | **`roundtrip_ms`** = now − t0 |

`roundtrip_ms` is measured entirely in the one `api` process, so it's free of cross-host clock
skew; the worker breakdown (queue wait / TTFT / generation) is informational. Filter logs or the
`wave:events` stream by a `corr_id` to see a single message's full journey and where time went.

## Graceful shutdown

- `api`: the FastAPI lifespan's exit path runs `aclose_analytics()` → `stop_logging()` →
  `close_redis()`.
- `worker` / `manager`: `main()` installs SIGTERM/SIGINT handlers; on signal it stops the
  workers/autoscaler, then flushes analytics + logs and closes Redis. The drain is time-bounded
  so shutdown can't hang. Result: **no data loss on a normal shutdown.**

## Config knobs (`app/config.py`)
`LOG_LEVEL`, `SLOW_OP_MS`, `ANALYTICS_QUEUE_MAX`, `ANALYTICS_WATERMARK`, `ANALYTICS_BATCH`,
`ANALYTICS_FLUSH_MS`, `EVENTS_STREAM_MAX`, `SHUTDOWN_DRAIN_S`.
