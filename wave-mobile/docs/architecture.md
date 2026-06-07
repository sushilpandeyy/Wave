# Wave Mobile — App Infrastructure Design

How the `wave-mobile` Expo/React Native app is structured to consume the Wave backend,
**and** the backend additions required before a real app can use it. Grounded in the actual
backend code (`regina/app/*`), not aspiration.

---

## 1. Backend reality & gap analysis

The backend (`regina/app/api.py`) is a message-processing *pipeline*, not a product API.
Today it exposes **three** HTTP surfaces:

| Surface | Purpose | App-usable? |
|---|---|---|
| `WS /ws/chat?user_id=<uuid>` | the chat — streams Wave's reply token-by-token | ✅ the core |
| `GET /healthz` | liveness | infra only |
| `GET /metrics` | queues/pressure/pool/analytics | ops dashboard only |

Everything else the app needs lives in Postgres (`users`, `personalities`, `sessions`,
`messages`) but is **not exposed over HTTP**. The gaps that block a real app:

| Need | Status today | Who builds it |
|---|---|---|
| Sign up / log in / identity | ❌ none — WS trusts a raw `user_id` query param | **backend** (new REST + auth) |
| Load past messages on open | ❌ history only feeds the LLM, never returned to clients | **backend** (`GET /sessions/{id}/messages`) |
| List past conversations | ❌ `sessions` table unexposed | **backend** (`GET /sessions`) |
| Read my profile / tier | ❌ unexposed | **backend** (`GET /me`) |
| View the companion's persona | ❌ `personalities` unexposed | **backend** (`GET /me/personality`) |
| Upgrade tier / billing | ❌ `tier` is just a column | **backend** (billing + webhook) |
| Push / re-engagement | ❌ none | **backend** (push tokens + sender) |
| Secure the chat socket | ⚠️ unauthenticated; spoofable `user_id` | **backend** (token-auth the WS) |

> **The #1 thing to fix:** the WS is unauthenticated — anyone who knows/guesses a UUID can
> chat as that user. Auth must land before this ships beyond a demo.

### The chat WebSocket contract (what the app codes against)

Client → server (text frame):
```jsonc
{ "message": "hey, how's it going?" }
```

Server → client frames (`app/api.py` + `app/streaming.py`):
```jsonc
{ "type": "token",  "value": "wait" }          // many of these, in order
{ "type": "done",   "mood": "upbeat" }         // turn complete; mood is free-form
{ "type": "notice", "message": "okay okay…" }  // in-character interruption (NOT an error)
```

Notices replace error popups — they're Wave speaking (`app/voice.py`): `rate_limited`,
`approaching`, `overloaded`, plus `"Unknown user."` for a bad id. Safety flags
(`jailbreak/nsfw/boundary/crisis`) are handled server-side and arrive as normal
tokens + a `done` — the app shows them like any reply. **Mood** is an open vocabulary
(examples: `neutral, tender, upbeat, playful, anxious, excited`). **Tiers**: `free`,
`premium`, `premium++` — they differ only in priority, context depth, and how gracefully
they degrade under load; the app should never expose that as "errors."

---

## 2. Target system architecture

```
┌──────────────────────── wave-mobile (Expo / RN) ────────────────────────┐
│  Presentation     Auth · Chat · History · Profile · Paywall  (screens)  │
│  State            Zustand stores  +  TanStack Query (server cache)       │
│  Domain           User · Session · Message · Persona · Tier  (types)     │
│  Data access      RestClient (fetch+auth)   WsChatClient (stream)        │
│  Platform         SecureStore · SQLite cache · Notifications · Updates   │
└───────────────┬───────────────────────────────┬────────────────────────┘
                │ HTTPS (REST, to be built)      │ WSS (exists)
        ┌───────▼────────┐              ┌─────────▼─────────┐
        │  Wave REST API │              │  Wave WS /ws/chat │
        │  (NEW gateway) │              │  (FastAPI api.py) │
        └───────┬────────┘              └─────────┬─────────┘
                └──────────┬──────────────────────┘
                  Postgres · Redis · worker pool · reflector
```

The app talks to **two** channels: a (to-be-built) **REST gateway** for everything
request/response (auth, history, profile, billing) and the **existing WebSocket** for the
live chat stream.

---

## 3. Backend work required (the "what needs to be made")

A thin **REST gateway** alongside the existing FastAPI app. All read endpoints reuse the
queries already in `app/queries.py`.

1. **Auth & identity**
   - `POST /auth/register` → create `User` (+ empty `Personality`), return tokens.
   - `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`.
   - JWT access token whose subject **is** the `user_id`. Issued by the gateway.
2. **Secure the WS** — replace `?user_id=` with `?token=` (or `Authorization` on connect);
   `ws_chat` validates the JWT and derives `user_id` from it instead of trusting the param.
   (Single, surgical change in `api.py`.)
3. **Read APIs** (back the app's screens; thin wrappers over `queries.py`):
   - `GET /me` → profile + tier (`User`).
   - `GET /me/personality` → traits + summary (`get_personality`).
   - `GET /sessions?cursor=…` → conversation list (`sessions`, newest first).
   - `GET /sessions/{id}/messages?cursor=…` → paginated transcript (`recent_messages`).
   - `PATCH /me` → display_name, locale, timezone, settings.
4. **Subscription / billing**
   - `GET /me/subscription`, `POST /me/subscription/checkout` (Stripe/RevenueCat).
   - Billing **webhook** flips `users.tier`; the app re-fetches `/me`.
5. **Push / re-engagement**
   - `POST /me/devices` registers an Expo push token.
   - A sender (e.g. fired by the existing `reflector`/reaper) nudges idle users in-voice.

Keep the hot chat path untouched — the gateway is request/response only; streaming stays
on the WS exactly as it is.

---

## 4. Mobile app architecture

**Stack:** Expo SDK 56 · expo-router (file-based nav, already scaffolded) · TypeScript ·
React 19. Layered so the chat-streaming complexity is isolated from UI.

### Folder structure (under `wave-mobile/src/`)
```
app/                      # expo-router routes (screens)
  (auth)/                 #   login, register, onboarding
  (tabs)/                 #   chat, history, profile  (authed)
  paywall.tsx             #   upgrade flow
  _layout.tsx             #   root: auth gate + providers
features/
  chat/                   # the heart of the app
    WsChatClient.ts       #   socket lifecycle, frames → events
    useChat.ts            #   hook: send(), streaming reply, notices
    chatStore.ts          #   Zustand: messages, status, streaming buffer
    components/           #   MessageList, Bubble, StreamingBubble, Composer, NoticeBubble
  history/                # session list + open a past conversation
  profile/                # tier badge, persona view, settings
  auth/                   # token storage, refresh, AuthProvider
data/
  rest.ts                 # typed REST client (auth header, refresh-on-401)
  queries.ts              # TanStack Query hooks (sessions, me, personality)
  db.ts                   # expo-sqlite: local message cache
domain/
  types.ts                # User, Session, Message, Persona, Tier, Mood, frames
config/
  env.ts                  # API_BASE / WS_BASE per environment
theme/
  mood.ts                 # mood → accent color/animation
```

### Core modules

**`WsChatClient`** — the most important piece.
- Connects to `WSS .../ws/chat?token=…`; one socket per active chat.
- Parses frames into a typed event stream: `onToken`, `onDone(mood)`, `onNotice(msg)`.
- **Reconnect with backoff** (network drops, app foreground/background via `AppState`).
- **Mid-stream resilience**: if the socket drops before `done`, mark the partial assistant
  bubble "interrupted" and offer resend (server has no per-turn resume — `message_id`
  correlates, but the app owns recovery).
- Single in-flight turn per connection (matches the WS's one-turn-at-a-time loop).

**`chatStore` (Zustand)** holds: ordered `messages[]`, `connectionStatus`, the live
`streamingText` buffer, and `pendingNotices[]`. The streaming token buffer lives here so
the UI re-renders cheaply as tokens arrive.

**`rest.ts`** — typed `fetch` wrapper: injects the access token, transparently refreshes on
`401`, surfaces typed errors. Feeds TanStack Query for caching/pagination of history & profile.

**Auth** — tokens in `expo-secure-store` (Keychain/Keystore). `AuthProvider` gates the
router: no token → `(auth)` stack; token → `(tabs)`.

**Local cache** — `expo-sqlite` mirrors recent messages/sessions so the chat opens
instantly and survives offline; REST history hydrates/reconciles it on reconnect.

---

## 5. Screens & navigation

| Route | Screen | Backend it uses |
|---|---|---|
| `(auth)/onboarding` | welcome + value prop | — |
| `(auth)/login` · `register` | identity | `POST /auth/*` *(new)* |
| `(tabs)/chat` | **main** — streaming chat with Wave | `WS /ws/chat` + history REST |
| `(tabs)/history` | past conversations, tap to reopen | `GET /sessions`, `/sessions/{id}/messages` *(new)* |
| `(tabs)/profile` | tier badge, persona, settings, sign out | `GET /me`, `/me/personality`, `PATCH /me` *(new)* |
| `paywall` | upgrade free→premium→premium++ | `…/subscription/checkout` *(new)* |

The scaffold's `index.tsx`/`explore.tsx` tabs get replaced by `chat`/`history`/`profile`.

---

## 6. Data flow — one chat message

```
User types → optimistic USER bubble appended (chatStore)
          → WsChatClient.send({message})
          ← token, token, token …    → append to streamingText → live ASSISTANT bubble
          ← done {mood}              → finalize bubble, tag mood, theme accent, clear buffer
          (or) ← notice {message}    → render NoticeBubble in Wave's voice (no error UI)
          (or) socket drop pre-done  → mark "interrupted", offer resend
On open:  REST GET history (cache-first via SQLite) → then connect WS for new turns
```

Persistence is automatic server-side: the API persists the user message, the worker
persists Wave's reply with its mood. The app only needs to *render* and *cache*.

---

## 7. Cross-cutting concerns

- **Mood-reactive UI** — `done.mood` / `message.mood` drive a subtle accent color and
  send-animation (`theme/mood.ts`), with a neutral fallback for unknown moods. Makes Wave
  feel alive without changing copy.
- **Tier as delight, never error** — show a small tier badge; gate premium polish behind
  the paywall. Degradation/shedding arrives as in-voice **notices**, so the app must render
  notices as Wave talking — *never* a red error toast. This is the backend's whole philosophy.
- **Safety** — crisis/boundary replies arrive as normal text; render them with care
  (e.g. a gentle resources affordance for `crisis`) but no special protocol is needed.
- **Reconnection** — backoff + resume on foreground; queue an unsent message offline and
  flush on reconnect.
- **Observability** — client crash/error reporting (Sentry); optionally echo the turn's
  `message_id` as a client correlation id so app traces line up with the backend's
  `corr_id` round-trip logs.

---

## 8. Build, deploy & infra

- **Env config** (`config/env.ts`): `API_BASE` + `WS_BASE` via `EXPO_PUBLIC_*` /
  `app.config.ts` extra, per `dev | staging | prod`. Local dev points at the Docker
  Compose backend (`ws://<LAN-IP>:8000/ws/chat`; `localhost` won't reach a device).
- **Builds**: EAS Build (iOS/Android), EAS Submit to stores.
- **OTA**: `expo-updates` for JS-only ships between native builds.
- **Backend** stays on the existing Docker Compose / GCP target; the new REST gateway is
  another FastAPI router in the same image (or a sibling service) behind the LB.
- ⚠️ **Local-toolchain note**: the Android build needs JDK 21 + the foojay pin (see
  project setup notes) — unrelated to app code.

---

## 9. Phased roadmap

| Phase | Goal | Backend | App |
|---|---|---|---|
| **0 — Spike** | chat works end-to-end | use seeded `user_id` | `WsChatClient` + chat screen + composer + streaming bubble + notices |
| **1 — Identity** | real users, secure socket | auth REST + JWT-on-WS | auth screens, SecureStore, router gate |
| **2 — Continuity** | history & profile | read APIs | history list, transcript, profile, persona, SQLite cache |
| **3 — Monetize** | tiers | subscription + webhook | paywall, tier badge, upgrade |
| **4 — Retention** | bring users back | push sender (via reaper) | push registration, deep links, mood polish |

**Start at Phase 0** against a seeded user — it proves the streaming UX (the hard part)
with zero backend changes — then layer auth and the read APIs underneath.
```
