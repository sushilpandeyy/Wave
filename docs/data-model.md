# Data Model — indexes & queries

Detail for Part 1. The schema and the design decisions live in the
[README](../README.md); this doc covers the indexes and the performance-critical
query patterns.

## Indexes

| Index | What it speeds up |
|---|---|
| `sessions (user_id) WHERE status='active'` — partial unique | current active session, and enforces ≤1 active per user |
| `messages (session_id, created_at)` | recent messages for LLM context |
| `sessions (user_id, last_message_at)` | a user's session history |
| `users (tier, last_active_at)` | aggregations by tier / active users by tier |
| `personalities (user_id)` — unique | the user's personality |

## Query patterns

### 1. Current active session for a user
```sql
SELECT * FROM sessions WHERE user_id = $1 AND status = 'active';
```
One-row hit on the partial unique index. The index only contains active sessions, so its
size scales with *active users*, not total session history.

### 2. Recent N messages for context
`N` is the tier's context budget — higher tiers read more history, lower tiers fewer
(graceful degradation is just a smaller `LIMIT`).
```sql
SELECT role, content, created_at
FROM messages
WHERE session_id = $1
ORDER BY created_at DESC
LIMIT $2;
```
The index is already ordered by `(session_id, created_at)`, so Postgres range-scans the
session's tail and stops after N rows — no sort, and it never touches other sessions'
messages.

### 3. Aggregation by tier
e.g. active users in the last 24h:
```sql
SELECT tier, count(*)
FROM users
WHERE last_active_at >= now() - interval '24 hours'
GROUP BY tier;
```
Served from `users (tier, last_active_at)` as an index-only scan — no heap lookups, no
joins. `tier` is also denormalized onto `messages`, so per-tier message counts work the
same way without joining back to `users`.
