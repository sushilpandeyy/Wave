# Session-end Personality Reflection

When a conversation episode ends, Wave learns from it: a background pipeline runs an LLM
"reflection" over the transcript and **gently** evolves the user's personality
(`personalities.traits` + `personalities.summary`). It runs in the `reflector` service —
entirely off the chat hot path — and a bad LLM response can never corrupt the stored model.

## 1. When a session "ends" — the reaper

A session is an episode bounded by inactivity. The **`SessionReaper`** loop claims idle
sessions atomically:

```sql
UPDATE sessions SET status='closed'
WHERE status='active' AND last_message_at < now() - :idle_timeout
RETURNING id, user_id, message_count
```

`UPDATE … RETURNING` means each session is claimed by exactly one reflector instance (safe to
run many). Sessions with `message_count >= REFLECT_MIN_MESSAGES` are pushed onto the Redis list
`wave:reflect`; trivial ones are just closed.

## 2. Decoupled consumers

A pool of `REFLECT_CONCURRENCY` consumers `BRPOP` the queue and reflect, so LLM cost / provider
load stays bounded and detection stays cheap. A failed reflection is re-enqueued once, then
dropped (logged as `reflect_failed`).

## 3. The LLM call

`build_reflection_messages(personality, transcript)` gives the model the **previous** traits +
summary and the conversation, and asks it to *nudge, not overhaul*, returning strict JSON via
`llm.complete(..., json=True)` (OpenAI JSON mode; the mock returns canned JSON offline):

```json
{"traits": {"warmth":0-1, "humor":..., "openness":..., "formality":...,
            "playfulness":..., "supportiveness":...},
 "summary": "updated long-term memory, concise & factual",
 "title": "short label for this conversation"}
```

## 4. Validate + merge (never corrupt the personality)

- Parse defensively — on **any** parse/LLM failure the old personality is left untouched.
- Traits: keep only known `TRAIT_KEYS`, clamp to `[0,1]`, and **blend with the old value via a
  learning rate** `α` (`TRAIT_ALPHA`, default 0.3): `new = (1-α)·old + α·proposed`. The persona
  evolves gradually and is immune to one weird conversation (verified: 0.8 → 0.815, not → 0.85).
- Summary: the model's merged summary, trimmed to `SUMMARY_MAX_CHARS`; falls back to the old one.

## 5. Persist

`update_personality(user_id, traits, summary)` upserts the single personality row (updated in
place — `updated_at` bumps). The conversation's recap is stored on `sessions.title`. (One row
per user; versioned history is a possible future extension.)

## Observability & shutdown
corr_id = `session_id`. Events: `session_closed`, `reflect_enqueued`, `reflect_started`,
`reflect_completed` (with the new traits), `reflect_failed`; the LLM call is wrapped in
`obs.timed("reflect_llm")`. The `reflector` shuts down gracefully on SIGTERM — it stops between
jobs (no reflection cut off mid-call) and flushes analytics + logs.

## Config (`app/config.py`)
`SESSION_IDLE_TIMEOUT_S`, `REAPER_INTERVAL_S`, `REFLECT_MIN_MESSAGES`, `REFLECT_CONCURRENCY`,
`REFLECT_MSG_LIMIT`, `TRAIT_ALPHA`, `SUMMARY_MAX_CHARS`.
