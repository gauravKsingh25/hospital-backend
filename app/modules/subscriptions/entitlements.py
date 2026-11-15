"""What a hospital may use right now. Pure: no database, no clock of its own.

Status is derived from the paid period and the time the caller passes in, so
a hospital whose month ran out at 02:00 is in grace at 02:00, not when the
next background sweep happens to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.modules.subscriptions.models import SubscriptionStatus

__all__ = ["Entitlements", "effective_status"]

_USABLE = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE})


def effective_status(
    *,
    period_end: datetime | None,
    cancel_at_period_end: bool,
    now: datetime,
    grace_days: int,
) -> SubscriptionStatus:
    """The subscription's status at `now`.

    No end date is a complimentary subscription and never lapses. A hospital
    that cancelled gets no grace: it asked to stop at the end of the month.
    """
    if period_end is None or now <= period_end:
        return SubscriptionStatus.ACTIVE
    if cancel_at_period_end:
        return SubscriptionStatus.CANCELLED
    if now <= period_end + timedelta(days=grace_days):
        return SubscriptionStatus.GRACE
    return SubscriptionStatus.EXPIRED


@dataclass(frozen=True, slots=True)
class Entitlements:
    plan_code: str
    status: SubscriptionStatus
    period_end: datetime | None
    complimentary: bool
    features: frozenset[str]

    @property
    def usable(self) -> bool:
        """The hospital may work: paid up, or inside the grace window."""
        return self.status in _USABLE

    def has(self, feature: str) -> bool:
        return self.usable and feature in self.features

    def days_left(self, now: datetime) -> int | None:
        """Whole days of the paid period remaining; None when it never ends."""
        if self.period_end is None:
            return None
        return max((self.period_end - now).days, 0)
