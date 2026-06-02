"""Wave's voice — personality-aware copy + an anti-spam notice gate.

Wave never speaks like a machine. When something has to interrupt the conversation
(rate limit, overload, a safety boundary), we say it in her warm, gently cute
companion voice. Lines are kept in pools and chosen at random so it never feels
canned, and the NoticeGate makes sure we say each kind of thing at most once per
cooldown — so the user is never spammed.
"""

import random

from redis.asyncio import Redis

from app.config import settings

# Pools of in-character lines, picked at random. Warm and gently playful — the cuteness
# is in the wording, not a pile of emojis. `crisis` is the exception: sincere and grounded.
LINES: dict[str, list[str]] = {
    "approaching": [
        "ooh, we're really going back and forth — I love it. let's catch our breath for a sec?",
        "you've got my whole attention. let's slow down just a touch so I can keep up?",
    ],
    "rate_limited": [
        "okay okay, I need a tiny breather — give me a moment and I'm all yours again.",
        "phew, you're quick! let me catch up — back in just a sec, promise.",
        "I'm a little overwhelmed right now — let's pause for a breath and pick this right back up.",
    ],
    "overloaded": [
        "I'm feeling a little under the weather right now — try me again in a moment?",
        "so many people need me at once — let me catch my breath and I'll be right back.",
    ],
    "jailbreak": [
        "haha, nice try — but I'm just me. what's actually on your mind?",
        "mm, I'd rather stay myself with you. let's talk about something real instead?",
    ],
    "nsfw": [
        "mm, that's not really my thing — let's keep it sweet? I'm still here for you.",
        "I'd rather not go there, but I'm always up for a good chat about anything else.",
    ],
    "boundary": [
        "I can't help with that one — but I'm right here. wanna talk about something else?",
        "that's a little out of my depth, but I'm not going anywhere. what else is up?",
    ],
    "crisis": [
        "hey, I'm really glad you told me. you matter, and you don't have to carry this alone — "
        "please reach out to someone who can be with you right now. I'm here too.",
    ],
    "output_blocked": [
        "hmm, that came out wrong — let me gather my thoughts and try again in a bit.",
    ],
    "error": [
        "oof, something hiccupped on my end — mind trying that again?",
    ],
}


def say(kind: str) -> str:
    """A fresh in-character line for a scenario."""
    return random.choice(LINES[kind])


class NoticeGate:
    """Per-user, per-kind cooldown so non-conversational notices never spam.

    `allow()` returns True the first time a kind fires for a user, then False until
    the cooldown lapses — one short Redis op, only on the path where we'd speak.
    """

    def __init__(self, redis: Redis):
        self._redis = redis

    async def allow(self, user_id: str, kind: str) -> bool:
        # SET NX EX: succeeds (returns True) only if no live cooldown key exists.
        key = f"wave:notice:{user_id}:{kind}"
        return bool(await self._redis.set(key, "1", nx=True, ex=settings.notice_cooldown_s))
