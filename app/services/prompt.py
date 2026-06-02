"""Prompt construction for the companion.

Assembles the message list fed to the completion client: a (placeholder) system
prompt, the persona derived from the user's Personality row, and a tier-trimmed
slice of recent conversation history.
"""

from app.core.tiers import TierPolicy
from app.models.chat import Chat, MessageType
from app.models.personality import Personality
from app.services.llm import Message

# Placeholder — the real Wave system prompt will be defined later.
DUMMY_SYSTEM_PROMPT = (
    "You are Wave, a warm, attentive AI companion. Speak naturally and kindly, "
    "like a close friend who listens well. Keep replies human, never robotic."
)

_ROLE_BY_TYPE = {
    MessageType.USER: "user",
    MessageType.ASSISTANT: "assistant",
    MessageType.SYSTEM: "system",
}


def _persona_block(personality: Personality | None) -> str | None:
    if personality is None:
        return None
    parts: list[str] = []
    if personality.traits:
        traits = ", ".join(f"{k}={v}" for k, v in personality.traits.items())
        parts.append(f"Personality traits: {traits}.")
    if personality.context:
        parts.append(f"What you remember about them: {personality.context}")
    return " ".join(parts) if parts else None


def build_prompt(
    *,
    personality: Personality | None,
    history: list[Chat],
    user_message: str,
    policy: TierPolicy,
) -> list[Message]:
    """Build the messages list. History is trimmed to the tier's context budget."""
    messages: list[Message] = [{"role": "system", "content": DUMMY_SYSTEM_PROMPT}]

    persona = _persona_block(personality)
    if persona:
        messages.append({"role": "system", "content": persona})

    # Keep only the most recent N turns allowed for this tier (graceful degrade).
    recent = history[-policy.max_context_messages :] if history else []
    for chat in recent:
        messages.append(
            {"role": _ROLE_BY_TYPE[chat.messagetype], "content": chat.message}
        )

    messages.append({"role": "user", "content": user_message})
    return messages
