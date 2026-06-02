"""Tier policies and pressure-driven degradation.

Tiers differ ONLY by priority (who is served first), reserved capacity (pool.py), and
how readily they degrade. A single global `pressure` level (0..3) decides who gives way,
always lowest-tier-first; enterprise (premium++) never degrades and is never shed.

Everything tier-specific lives in one `POLICIES` table; `degrade()` is pure arithmetic
over it, so there are no per-(tier, pressure) branches to maintain.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Tier

# Model classes, best -> fastest. Degrading steps down this ladder.
MODEL_LADDER = ("premium", "standard", "fast")


@dataclass(frozen=True)
class TierPolicy:
    lane: str          # Redis queue lane
    priority: int      # lower = served first
    base_context: int  # recent messages fed to the model at rest
    base_model: int    # starting index into MODEL_LADDER
    resilience: int    # pressure levels absorbed before degrading (large = never)
    rpm: int           # sustained requests/min (token bucket refill rate)
    burst: int         # bucket size — how many back-to-back messages are fine


POLICIES: dict[Tier, TierPolicy] = {
    # Enterprise limits are set so high it is effectively never rate-limited.
    Tier.PREMIUM_PLUS: TierPolicy("ent", 0, 50, 0, resilience=99, rpm=600, burst=120),
    Tier.PREMIUM: TierPolicy("prem", 1, 25, 1, resilience=1, rpm=60, burst=20),
    Tier.FREE: TierPolicy("free", 2, 8, 2, resilience=0, rpm=15, burst=5),
}

LANE: dict[Tier, str] = {t: p.lane for t, p in POLICIES.items()}
# Lanes highest-priority first — the dequeue scan order (enterprise fast-track).
LANES_BY_PRIORITY: list[str] = [
    POLICIES[t].lane for t in sorted(POLICIES, key=lambda t: POLICIES[t].priority)
]


@dataclass(frozen=True)
class Effective:
    """What a worker actually uses for one job. Shedding (rejecting free) happens at
    admission; by the time a job reaches a worker it is always served — just leaner."""

    max_context: int
    model_quality: str


def degrade(tier: Tier, pressure: int) -> Effective:
    """Each tier absorbs `resilience` pressure levels, then degrades one step per level:
    context halves and the model drops down MODEL_LADDER. No I/O, no branching per tier."""
    p = POLICIES[tier]
    steps = max(0, pressure - p.resilience)
    context = p.base_context >> steps
    model = MODEL_LADDER[min(len(MODEL_LADDER) - 1, p.base_model + steps)]
    return Effective(context, model)
