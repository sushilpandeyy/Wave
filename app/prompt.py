"""Prompt assembly.

The prompt is a layered message list:
1. who Wave is (static persona),
2. who *this user* is — their hard traits + long-term memory (`Personality`),
3. a short cue for the current mood / scenario so the reply meets them where they are,
4. the tier-trimmed recent conversation,
5. the new message.
"""

from app.llm import Message
from app.models import Message as ChatMessage
from app.models import Personality

_SYSTEM = (
    "You are Wave, a warm, attentive AI companion. Speak naturally and kindly, like a "
    "close friend who listens well. Keep replies human, never robotic."
)

# Short tone cues per detected mood. Empty = no extra steering.
_MOOD_CUE = {
    "tender": "They seem to be in a tender, vulnerable place right now — be gentle, slow, and present.",
    "upbeat": "They seem upbeat and happy — match their warmth and share the moment.",
    "neutral": "",
}


def _persona_line(personality: Personality | None) -> str | None:
    if personality is None:
        return None
    parts = []
    if personality.traits:
        parts.append(
            "Personality: " + ", ".join(f"{k}={v}" for k, v in personality.traits.items())
        )
    if personality.summary:
        parts.append(f"What you remember about them: {personality.summary}")
    return " ".join(parts) or None


def _situation_line(mood: str, is_new_conversation: bool) -> str | None:
    parts = []
    cue = _MOOD_CUE.get(mood)
    if cue:
        parts.append(cue)
    if is_new_conversation:
        parts.append("This is the start of a fresh conversation — greet them warmly.")
    return " ".join(parts) or None


def build_prompt(
    *,
    personality: Personality | None,
    history: list[ChatMessage],
    user_message: str,
    mood: str = "neutral",
) -> list[Message]:
    """History is already trimmed to the tier's context budget by the caller."""
    messages: list[Message] = [{"role": "system", "content": _SYSTEM}]

    persona = _persona_line(personality)
    if persona:
        messages.append({"role": "system", "content": persona})

    situation = _situation_line(mood, is_new_conversation=not history)
    if situation:
        messages.append({"role": "system", "content": situation})

    for m in history:
        messages.append({"role": m.role.value, "content": m.content})
    messages.append({"role": "user", "content": user_message})
    return messages
