"""Subscription status is a function of the clock — no database needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.subscriptions.entitlements import Entitlements, effective_status
from app.modules.subscriptions.models import SubscriptionStatus

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
D = timedelta(days=1)


@pytest.mark.parametrize(
    ("end", "cancel", "expected"),
    [
        (None, False, "ACTIVE"),
        (NOW + D, False, "ACTIVE"),
        (NOW, False, "ACTIVE"),
        (NOW - D, False, "GRACE"),
        (NOW - 7 * D, False, "GRACE"),
        (NOW - 7 * D - timedelta(seconds=1), False, "EXPIRED"),
        (NOW + D, True, "ACTIVE"),
        (NOW - timedelta(seconds=1), True, "CANCELLED"),
    ],
)
def test_effective_status(end: datetime | None, cancel: bool, expected: str) -> None:
    status = effective_status(period_end=end, cancel_at_period_end=cancel, now=NOW, grace_days=7)
    assert status == expected


def test_expired_entitlements_have_no_features() -> None:
    e = Entitlements(
        "professional", SubscriptionStatus.EXPIRED, NOW - 30 * D, False, frozenset({"ipd"})
    )
    assert not e.usable
    assert not e.has("ipd")


def test_grace_keeps_features() -> None:
    e = Entitlements("professional", SubscriptionStatus.GRACE, NOW - D, False, frozenset({"ipd"}))
    assert e.usable
    assert e.has("ipd")
    assert not e.has("diagnostics")


def test_days_left() -> None:
    active = Entitlements(
        "standard",
        SubscriptionStatus.ACTIVE,
        NOW + timedelta(days=2, hours=1),
        False,
        frozenset({"opd"}),
    )
    assert active.days_left(NOW) == 2
    lapsed = Entitlements("standard", SubscriptionStatus.EXPIRED, NOW - 9 * D, False, frozenset())
    assert lapsed.days_left(NOW) == 0
    forever = Entitlements("professional", SubscriptionStatus.ACTIVE, None, True, frozenset())
    assert forever.days_left(NOW) is None
