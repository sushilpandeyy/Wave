"""Safety handling: screen inbound and outbound text.

Boilerplate with a pluggable check. Replace `_classify` with a real moderation
model / provider call. The contract stays: return a SafetyVerdict the pipeline
can act on without leaking technical details to the user.
"""

import enum
from dataclasses import dataclass


class SafetyAction(str, enum.Enum):
    ALLOW = "allow"
    BLOCK = "block"
    SAFE_FALLBACK = "safe_fallback"   # respond with a gentle, redirecting reply


@dataclass
class SafetyVerdict:
    action: SafetyAction
    categories: list[str]
    # User-facing message — warm, never robotic — used when not ALLOW.
    user_message: str | None = None


_BLOCKLIST = {"selfharm_example", "abuse_example"}  # placeholder


class SafetyService:
    async def screen_input(self, text: str) -> SafetyVerdict:
        return await self._classify(text)

    async def screen_output(self, text: str) -> SafetyVerdict:
        return await self._classify(text)

    async def _classify(self, text: str) -> SafetyVerdict:
        lowered = text.lower()
        hits = [c for c in _BLOCKLIST if c in lowered]
        if hits:
            return SafetyVerdict(
                action=SafetyAction.SAFE_FALLBACK,
                categories=hits,
                user_message=(
                    "I want to make sure you're okay. I can't help with that, "
                    "but I'm here to talk."
                ),
            )
        return SafetyVerdict(action=SafetyAction.ALLOW, categories=[])
