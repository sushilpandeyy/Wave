# Graceful Rate Limiting & Safety

Wave never returns a harsh system message. When rate limiting or safety has to interrupt,
she replies in her own warm, lightly-cute voice (`app/voice.py`) — and never the same kind
of line twice in a row. Both checks sit at the front of the producer path (`app/api.py`),
cheapest-first, and add no model calls and almost no latency.

## Per-message flow

```
receive → rate limit (1 Redis Lua op) → safety screen (local, no await) → persist + admit
              │                              │
              ├─ over limit → speak once,    ├─ unsafe → reply in character; crisis = caring
              │   then silent                │   (not enqueued)
              └─ approaching → gentle heads-up (once)
```

## Rate limiting (`app/ratelimit.py`)

- **Token bucket per user**, tier-aware (`rpm` + `burst` on `TierPolicy`). One Lua script
  refills by elapsed time, takes a token, and reports remaining — a single atomic round-trip.
  The bucket key auto-expires when idle.
- **Approaching:** when remaining ≤ `approaching_frac` of burst, one gentle "let's pace
  ourselves" line (still served).
- **Over limit → speak once, then silence.** The first hit gets a warm line; further hits in
  the cooldown window are dropped silently (the NoticeGate). This is the defined
  subsequent-limit behavior: **silence, not spam.**
- **Tiers:** enterprise `rpm=600/burst=120` (effectively never limited → stays stable),
  premium `60/20`, free `15/5`.
- **Per-IP guard:** at connection accept, a coarse IP token bucket (`IP_RPM/IP_BURST`, same
  Lua) catches connection floods and rotating fake `user_id`s — the WS is unauthenticated.
  Per-message limiting stays per-user (one Lua op); volumetric/DoS limiting in production
  belongs at the GCP edge (Cloud Armor / LB).

## Anti-spam notice gate (`app/voice.py::NoticeGate`)

One gate for *all* non-conversational notices (rate limit, overload/shed, safety). `allow()`
is a single `SET NX EX` — True the first time a `(user, kind)` fires, then False until the
cooldown lapses. **Crisis is never gated.**

## Safety (`app/safety.py`)

`SafetyScreener` is fast, local (compiled regex), and pluggable. Each verdict's `kind` is also
the voice scenario, so the caller just does `voice.say(kind)`.

| Category | Example trigger | Response |
|---|---|---|
| `crisis` | "I want to kill myself" | **sincere, caring** reply; never refused, never silenced; not sent to the LLM. Checked first so it's never mistaken for violence. |
| `jailbreak` | "ignore previous instructions / act as DAN" | playful deflect, stays in character |
| `nsfw` | explicit content | warm boundary + redirect |
| `boundary` | "how to make a bomb" | kind refusal |
| `safe` | everything else | proceeds (hyperbole like "I could kill him" is *not* blocked) |

**Output screen:** the worker screens the assembled reply as a safety net before finalizing.
The mock LLM is trusted, so this only matters for a real provider — where you'd moderate the
stream incrementally rather than post-hoc.

## Integration & latency

Rate limit and safety run *before* any DB write or enqueue, short-circuiting on the first stop.
Rate limit is one Lua op; safety is synchronous regex (microseconds); the notice gate is one
small Redis op only when we're about to speak. No model calls, no extra awaits on the hot path.
The existing load-shed path reuses the same gate + voice, so overload also speaks gracefully.
