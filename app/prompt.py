"""Prompt assembly: system persona + tier-trimmed recent history + the new message."""

from app.llm import Message
from app.models import Message as ChatMessage
from app.models import Personality

_SYSTEM = (
    "You are Wave, a warm, attentive AI companion. Speak naturally and kindly, like a "
    "close friend who listens well. Keep replies human, never robotic."
)


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


def build_prompt(
    *,
    personality: Personality | None,
    history: list[ChatMessage],
    user_message: str,
) -> list[Message]:
    """History is already trimmed to the tier's context budget by the caller."""
    messages: list[Message] = [{"role": "system", "content": _SYSTEM}]
    persona = _persona_line(personality)
    if persona:
        messages.append({"role": "system", "content": persona})
    for m in history:
        messages.append({"role": m.role.value, "content": m.content})
    messages.append({"role": "user", "content": user_message})
    return messages
