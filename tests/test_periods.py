"""Subscription period maths (spec §7). Pure functions, a fixed clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.subscriptions import periods

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def test_early_renewal_extends_from_the_old_end() -> None:
    end = NOW + 5 * DAY
    start, new_end = periods.renewal_period(current_end=end, approved_at=NOW, interval_days=30)
    assert start == end
    assert new_end == end + 30 * DAY


def test_late_renewal_starts_at_approval() -> None:
    # Ended three days ago, in grace: the lapse is not paid for twice.
    start, new_end = periods.renewal_period(
        current_end=NOW - 3 * DAY, approved_at=NOW, interval_days=30
    )
    assert start == NOW
    assert new_end == NOW + 30 * DAY


def test_renewal_with_no_period_starts_at_approval() -> None:
    start, _ = periods.renewal_period(current_end=None, approved_at=NOW, interval_days=30)
    assert start == NOW


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [
        (15, 3000),  # ceil(5999 * 15 / 30) = ceil(2999.5)
        (10.5, 2100),  # ceil(5999 * 10.5 / 30) = ceil(2099.65)
        (30, 5999),
    ],
)
def test_upgrade_pro_rates_and_rounds_up(days_left: float, expected: int) -> None:
    amount = periods.upgrade_amount(
        old_price=3000,
        new_price=8999,
        now=NOW,
        period_end=NOW + timedelta(days=days_left),
        interval_days=30,
    )
    assert amount == expected


def test_upgrade_minimum_is_one_rupee() -> None:
    amount = periods.upgrade_amount(
        old_price=3000,
        new_price=8999,
        now=NOW,
        period_end=NOW + timedelta(seconds=1),
        interval_days=30,
    )
    assert amount == 1


def test_upgrade_after_the_period_ended_is_still_one_rupee_not_negative() -> None:
    amount = periods.upgrade_amount(
        old_price=3000, new_price=8999, now=NOW, period_end=NOW - DAY, interval_days=30
    )
    assert amount == 1


@pytest.mark.parametrize(
    ("offset_days", "expected"),
    [
        (8, None),
        (6.9, 7),
        (3.5, 7),
        (2.5, 3),
        (0.5, 1),
        (-0.1, 0),
        (-6.9, 0),
        (-7.1, -1),
    ],
)
def test_reminder_buckets(offset_days: float, expected: int | None) -> None:
    due = periods.reminder_due(
        period_end=NOW + timedelta(days=offset_days), now=NOW, grace_days=7, cancelled=False
    )
    assert due == expected


def test_a_cancelled_subscription_gets_no_reminders() -> None:
    assert (
        periods.reminder_due(period_end=NOW + 2 * DAY, now=NOW, grace_days=7, cancelled=True)
        is None
    )
