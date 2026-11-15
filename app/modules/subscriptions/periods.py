"""Subscription period maths (spec §7). Pure: the clock is always passed in.

A period is `interval_days` long (30), not a calendar month: "renewed on the
31st" then has one meaning. Money is whole rupees.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

__all__ = ["REMINDER_DAYS", "reminder_due", "renewal_period", "upgrade_amount"]

# Reminders before the period ends, largest first. 0 is "you are in grace",
# -1 "your subscription has ended": the buckets the reminder table stores.
REMINDER_DAYS: tuple[int, ...] = (7, 3, 1)
GRACE_BUCKET = 0
EXPIRED_BUCKET = -1

_SECONDS_PER_DAY = 86_400


def renewal_period(
    *, current_end: datetime | None, approved_at: datetime, interval_days: int
) -> tuple[datetime, datetime]:
    """The period a renewal pays for.

    Renewing early extends from the old end, so no paid day is lost. Renewing
    late — in grace, or after expiry — starts at approval, so the days the
    hospital went unpaid are not charged for twice.
    """
    start = approved_at if current_end is None else max(current_end, approved_at)
    return start, start + timedelta(days=interval_days)


def upgrade_amount(
    *,
    old_price: int,
    new_price: int,
    now: datetime,
    period_end: datetime,
    interval_days: int,
) -> int:
    """The difference in price for the part of the period still to run.

    `ceil((new - old) * remaining days / interval)`, with the remaining days
    counted to the second, and never less than ₹1: an upgrade on the last
    afternoon is still a payment that has to be checked.
    """
    remaining_days = max((period_end - now).total_seconds(), 0) / _SECONDS_PER_DAY
    amount = math.ceil((new_price - old_price) * remaining_days / interval_days)
    return max(amount, 1)


def reminder_due(
    *, period_end: datetime, now: datetime, grace_days: int, cancelled: bool
) -> int | None:
    """Which reminder bucket the subscription is in at `now`, if any.

    7 within the last week, 3 within three days, 1 within the last day, 0 once
    the period has ended (grace), -1 once grace is over. A cancelled
    subscription is told nothing: it chose to stop.
    """
    if cancelled:
        return None
    left = (period_end - now).total_seconds() / _SECONDS_PER_DAY
    if left <= -grace_days:
        return EXPIRED_BUCKET
    if left <= 0:
        return GRACE_BUCKET
    for days in reversed(REMINDER_DAYS):
        if left <= days:
            return days
    return None
