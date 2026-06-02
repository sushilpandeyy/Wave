"""Subscription tier definitions and per-tier service policies.

Everything that should *behave differently* per tier is centralized here so the
rest of the codebase reads a policy object rather than branching on tier names.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Tier(str, enum.Enum):
    FREE = "free"
    PREMIUM = "premium"
    PREMIUM_PLUS = "premium++"


class QueueLane(str, enum.Enum):
    """Which worker pool / priority lane a message is routed to."""

    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class TierPolicy:
    tier: Tier
    # Rate limiting (token bucket): sustained rate + burst allowance.
    requests_per_minute: int
    burst: int
    # Routing
    lane: QueueLane
    priority: int          # lower = served first within a lane
    # Graceful degradation knobs
    max_context_messages: int   # how much history we feed the model
    request_timeout_s: float
    # Model routing hint — pipeline picks a model class from this.
    model_quality: str          # "premium" | "standard" | "fast"


POLICIES: dict[Tier, TierPolicy] = {
    Tier.PREMIUM_PLUS: TierPolicy(
        tier=Tier.PREMIUM_PLUS,
        requests_per_minute=120,
        burst=30,
        lane=QueueLane.HIGH,
        priority=0,
        max_context_messages=50,
        request_timeout_s=30.0,
        model_quality="premium",
    ),
    Tier.PREMIUM: TierPolicy(
        tier=Tier.PREMIUM,
        requests_per_minute=60,
        burst=15,
        lane=QueueLane.HIGH,
        priority=1,
        max_context_messages=25,
        request_timeout_s=20.0,
        model_quality="standard",
    ),
    Tier.FREE: TierPolicy(
        tier=Tier.FREE,
        requests_per_minute=10,
        burst=3,
        lane=QueueLane.LOW,
        priority=2,
        max_context_messages=8,
        request_timeout_s=12.0,
        model_quality="fast",
    ),
}


def policy_for(tier: Tier) -> TierPolicy:
    return POLICIES[tier]


def resolve_tier(profile: dict | None) -> Tier:
    """Read the subscription tier from a user's `profile` JSON.

    Defaults to FREE when the profile is missing, malformed, or names an
    unknown tier — we never fail a request over a bad profile blob.
    """
    if not isinstance(profile, dict):
        return Tier.FREE
    try:
        return Tier(profile.get("tier"))
    except ValueError:
        return Tier.FREE
