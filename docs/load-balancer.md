# Tier-Aware Load Balancer

How Part 2 routes chat requests to worker pools by tier, stays stable under extreme
load, and degrades lowest-tier-first. Design goal throughout: **least latency, no
unnecessary awaits** — the request hot path is one Redis round-trip to enqueue and one
blocking dequeue.

## Execution model — pull, not push

Workers **pull**; there is no central dispatcher in the request path (which would add
state, awaits, and a single point of failure). The balancer's only job at request time is
*admit-or-shed + which lane* — done in one atomic Lua op. Workers then pull what they're
allowed to, in priority order.

- **Enqueue:** `LoadBalancer.admit()` runs a Lua script that reads live pressure, sheds
  free first if needed, else `ZADD`s the job — one round-trip, race-free (`app/balancer.py`).
- **Dequeue:** `BZPOPMIN` over an ordered list of lane keys — the order *is* the priority,
  and it blocks (no polling).

## Pools, lanes, workers

Three Redis sorted-set lanes, priority `enterprise (premium++) > premium > free`:

```
q:ent   q:prem   q:free
```

Three worker pools (`app/pool.py::POOLS`), kept warm by the supervisor:

| Pool | Pulls | Role |
|---|---|---|
| **priority** | `[q:ent]` only | fixed reserve that keeps enterprise stable under any load |
| **standard** | `[q:ent, q:prem, q:free]`, priority order | always-warm general capacity |
| **overflow** | same as standard | elastic burst the autoscaler ramps up under load, down when idle |

**No starvation:** standard/overflow workers mostly scan in strict priority, but a slice of
pulls (chance `fairness`) put the lowest lane first (`app/worker.py::_lanes`), so free always
drains. Combined with the priority pool keeping enterprise fast-tracked, every tier progresses.

## The four routing inputs

All four spec inputs collapse into one number — `pressure` — that admission and degradation
already read, so there is no second decision path:

| Input | How it feeds in |
|---|---|
| **tier** | picks the lane + the degradation curve |
| **system load** | `shared_backlog ÷ general_capacity` → a pressure level |
| **latency** | `latency_ewma ÷ latency_target` → a pressure level (the worst of load/latency wins) |
| **pool health** | error rate ≥ `error_circuit` trips a breaker → pressure 3 + stops scaling the failing pool |

## One pressure signal

There are no per-tier latency SLAs like "free < 10s". The autoscaler folds load, latency,
and health into a single `pressure` level (0–3) — `max(load_level, latency_level)`, forced to
3 if the circuit breaker is open — which decides who gives way, always lowest-tier-first.
Workers read it on each loop (pipelined with their heartbeat, so zero extra round-trips) and
apply it locally via `app/tiers.py::degrade`:

| pressure | free | premium | enterprise |
|---|---|---|---|
| 0 | full | full | full |
| 1 | smaller context | full | full |
| 2 | tiny context | smaller context | full |
| 3 | **shed at admission** | degraded | full |

"Degrade" = fewer context messages + a faster model. "Shed" = a warm soft-reject frame at
enqueue (never a stack trace), and only ever for free.

## Autoscaling within a budget

Budget is **one number: `W_MAX`** (max total concurrent workers ≈ cost). The autoscaler
(`app/pool.py::Autoscaler`, the `manager` service) runs a ~1s control loop:

1. read backlog + pool health + prune dead workers;
2. `priority` and `standard` stay at their fixed floors; only `overflow` is elastic:
   `overflow → min(W_MAX, floors + ceil(backlog / per_worker)) − floors`;
3. **ramp** `overflow` toward that demand by at most `ramp_step` per tick (gentle, no thrash);
   if the breaker is open, growth is frozen — a failing pool isn't handed more workers;
4. publish `wave:pool:target`, `wave:pressure`, `wave:circuit`.

Each `worker` container's `WorkerSupervisor` reads the target, takes a deterministic fair
share (so shares sum to exactly the target — `W_MAX` is a hard cap), and starts/stops worker
coroutines to match. Scaling is just asyncio tasks going up and down; no container spawning.

## Health → routing

Each worker writes heartbeat + in-flight + a latency EWMA + error rate to Redis
(`app/health.py`). These aren't just for dashboards: the autoscaler reads them every tick so
**latency** raises pressure and a high **error rate** trips a circuit breaker (pressure 3 +
frozen scaling). `GET /metrics` surfaces lane depths, pressure, `circuit_open`, pool targets,
live worker count, latency, and `pool_score`.

## Redis key map

`wave:q:{ent|prem|free}` (ZSET jobs) · `wave:pressure` (int) · `wave:circuit` (0/1) ·
`wave:pool:target` (hash priority/standard/overflow) · `wave:workers` (ZSET id→last-seen) ·
`wave:inflight` (int) · `wave:health` (hash latency/err) · `wave:containers` (ZSET) ·
`wave:stream:{msg_id}` (pub/sub).

## Verified behaviour

- **Functional:** every tier streams a reply; at rest, latency ordered premium++ < premium <
  free (quality-aware model speeds).
- **Enterprise stability:** with 111 free jobs backed up, an enterprise message completed in
  ~0.27s vs ~30s for free — **~112× faster**, because reserved workers serve `q:ent` only.
- **Tier-ordered shedding:** at pressure 3, free is rejected at admission while premium and
  enterprise are still admitted.
- **Budget cap + gentle scaling:** overflow ramps ±`ramp_step`/tick up to `W_MAX` and back to
  the floor when idle — never above budget, no thrash.
- **Health-/latency-aware routing:** a 9s latency EWMA alone raises pressure to 3; an error
  rate past `error_circuit` opens the breaker (pressure 3) and freezes overflow growth.
