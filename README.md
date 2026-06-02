# Wave

**Wave** is an AI companion chatbot with subscription tiers (`free`, `premium`,
`premium++`), built on FastAPI + PostgreSQL + Redis.

> This stage is the **database layer only** — schema, indexes, and the core queries
> for Part 1 (Data Modeling & Query Design). The rest of the pipeline comes later.

## Quickstart

You need a Postgres with a `wave` role and `wave` database (the default `POSTGRES_DSN`;
override it via env or `.env`).

```bash
# Local Postgres — no Docker:
pg_ctl -D .pgdata -l pg.log start    # first time: initdb -U wave -D .pgdata && createdb -U wave wave

pip install -r requirements.txt
python -m scripts.init_db            # creates tables + indexes
```

> Prefer Docker? `docker compose up -d` replaces the `pg_ctl` step.

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
