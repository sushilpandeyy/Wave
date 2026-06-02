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

## Safety (model-driven)

Detection lives in the model, not in regex — it's intent-aware instead of brittle keyword
matching. The system prompt (`app/prompt.py::WAVE_SYSTEM`) tells the model to begin every reply
with a control line:

```
META|mood=<word>|flag=<none|jailbreak|nsfw|boundary|crisis>
<the message>
```

The worker (`app/worker.py::_stream_reply`) peels that line off the stream:

| flag | Example | Response |
|---|---|---|
| `crisis` | "I want to kill myself" | drop the model's text, send the **sincere, caring** `voice.say("crisis")` |
| `jailbreak` / `nsfw` / `boundary` | jailbreak / explicit / harmful request | drop it, send the in-character `voice.say(flag)` |
| `none` | everything else | stream the body live; `mood` is recorded on the done frame + row |

So a non-`none` flag is exactly your "specific word → cute message" mapping. Mood is reported by
the model (no keyword guessing). Parsing is tolerant: if the model omits the line, the whole
output is treated as the reply (`mood=neutral`).

> Production hardening (not built): OpenAI's **moderation** endpoint as a cheap deterministic
> pre-gate, if you want a backstop that doesn't rely on the chat model self-reporting.

## Integration & latency

Rate limiting runs *before* any DB write or enqueue (one Lua op); the notice gate is one small
Redis op only when we're about to speak. Safety no longer adds anything to the producer path —
it rides along with generation in the worker (the model classifies + responds in one call), so
there's no extra model call and no pre-screen latency. The load-shed path reuses the same gate +
voice, so overload also speaks gracefully.
