"""
Tiered refresh scheduling.

With a free rate ceiling (~180 Graph calls/hour) and 1,000+ accounts, the
scheduler is the product. Polling everyone equally means a restaurant that
posts an event daily gets checked as rarely as one that hasn't posted since
2023.

Tiers are derived from observed posting cadence, so they self-correct: an
account that starts posting more gets promoted on the next cycle.

    hot     posted within the last 3 days   → every 12h
    warm    posted within the last 14 days  → every 24h
    cold    posted within the last 90 days  → every 96h
    dormant nothing in 90+ days             → every 2 weeks

Capacity check at the default cadence for 1,000 accounts with a typical
restaurant mix (~15% hot, 45% warm, 30% cold, 10% dormant):

    150 hot   × 2/day  = 300
    450 warm  × 1/day  = 450
    300 cold  × 0.25   =  75
    100 dorm  × 0.07   =   7
                       ≈ 832 calls/day   vs. 4,320/day available
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings

TIER_ORDER = ("hot", "warm", "cold", "dormant")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def classify(newest_posted_at: datetime | None, *, now: datetime | None = None) -> str:
    """Assign a tier from the most recent post's age."""
    if newest_posted_at is None:
        return "warm"  # unknown → give it a fair shot before demoting

    ref = now or _now()
    if newest_posted_at.tzinfo is None:
        newest_posted_at = newest_posted_at.replace(tzinfo=timezone.utc)

    age_days = (ref - newest_posted_at).days
    if age_days <= 3:
        return "hot"
    if age_days <= 14:
        return "warm"
    if age_days <= 90:
        return "cold"
    return "dormant"


def next_fetch_at(
    tier: str, *, now: datetime | None = None, jitter_frac: float = 0.15
) -> datetime:
    """
    Schedule the next fetch, spread out within the interval.

    The jitter matters operationally: without it every account seeded in the
    same batch comes due in the same second, producing a thundering herd that
    burns the hourly rate window in one burst and then idles.
    """
    import random

    ref = now or _now()
    hours = settings.TIER_INTERVALS_HOURS.get(tier, 24)
    spread = hours * jitter_frac
    return ref + timedelta(hours=hours + random.uniform(-spread, spread))


def promote_on_new_posts(current_tier: str, new_post_count: int) -> str:
    """A cold account that suddenly posts should be hot on the next pass."""
    if new_post_count <= 0:
        return current_tier
    idx = TIER_ORDER.index(current_tier) if current_tier in TIER_ORDER else 1
    return TIER_ORDER[max(0, idx - 1)]


def plan_capacity(
    tier_counts: dict[str, int], *, calls_per_hour: int | None = None
) -> dict[str, Any]:
    """
    Project daily call demand against the free rate ceiling.

    Run this before scaling the account list — it's the difference between
    discovering you're over budget now versus at 3am when the window locks.
    """
    per_hour = calls_per_hour or settings.IG_GRAPH_CALLS_PER_HOUR
    available = per_hour * 24

    demand = 0.0
    breakdown: dict[str, float] = {}
    for tier, count in tier_counts.items():
        hours = settings.TIER_INTERVALS_HOURS.get(tier, 24)
        per_day = (24.0 / hours) * count
        breakdown[tier] = round(per_day, 1)
        demand += per_day

    return {
        "dailyDemand": round(demand, 1),
        "dailyCapacity": available,
        "utilization": round(demand / available, 3) if available else None,
        "withinBudget": demand <= available,
        "breakdown": breakdown,
    }
