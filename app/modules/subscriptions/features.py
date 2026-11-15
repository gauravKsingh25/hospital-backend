"""The feature registry: what each plan includes.

One place. The backend gates routers with `require_feature`, services call
`service.ensure_feature`, and the frontend mirrors the keys. `plan_features`
in the database is seeded from this map and a test holds the two together.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = [
    "GATED_PREFIXES",
    "OPEN_PREFIXES",
    "PLAN_FEATURES",
    "PLAN_NAMES",
    "PLAN_PROFESSIONAL",
    "PLAN_STANDARD",
    "FeatureKey",
]


class FeatureKey(enum.StrEnum):
    # Patients, appointments, queue, consultation, OPD billing, admin setup.
    # In every plan; listed so "what does Standard include" has an answer.
    OPD = "opd"
    IPD = "ipd"
    DIAGNOSTICS = "diagnostics"
    MEDICINES = "medicines"
    ACCOUNTS = "accounts"
    MESSAGING = "messaging"
    REPORTS_FULL = "reports_full"


PLAN_STANDARD: Final[str] = "standard"
PLAN_PROFESSIONAL: Final[str] = "professional"

PLAN_NAMES: Final[dict[str, str]] = {
    PLAN_STANDARD: "Standard",
    PLAN_PROFESSIONAL: "Professional",
}

PLAN_FEATURES: Final[dict[str, frozenset[FeatureKey]]] = {
    PLAN_STANDARD: frozenset({FeatureKey.OPD}),
    PLAN_PROFESSIONAL: frozenset(FeatureKey),
}

# ---------------------------------------------------------------------------
# Which routes need which feature
# ---------------------------------------------------------------------------
# Paths are relative to the API prefix. The longest matching prefix wins, so
# `/ipd/medicines` is `medicines` although `/ipd` is `ipd`.
#
# `tests/test_api_feature_gate.py` walks every mounted route against these two
# maps: a route under a gated prefix must carry that feature's dependency, and
# a route under neither fails the build. A new router therefore cannot ship
# unclassified, which is the mistake this guards against: a Professional-only
# screen quietly reachable on Standard because nobody remembered to lock it.
GATED_PREFIXES: Final[dict[str, FeatureKey]] = {
    "/ipd": FeatureKey.IPD,
    "/ipd/medicines": FeatureKey.MEDICINES,
    "/diagnostics": FeatureKey.DIAGNOSTICS,
    "/notifications": FeatureKey.MESSAGING,
    "/billing/claims": FeatureKey.ACCOUNTS,
    # The accounts dashboard. `/billing/accounts` itself is the counter's
    # board and `/billing/accounts/{visit}` the bill at the desk: both OPD.
    "/billing/accounts/summary": FeatureKey.ACCOUNTS,
    "/billing/accounts/bills": FeatureKey.ACCOUNTS,
    "/billing/accounts/payments": FeatureKey.ACCOUNTS,
    "/billing/accounts/outstanding": FeatureKey.ACCOUNTS,
    "/billing/accounts/patients": FeatureKey.ACCOUNTS,
    "/reports/occupancy": FeatureKey.REPORTS_FULL,
    "/reports/inpatient": FeatureKey.REPORTS_FULL,
    "/reports/follow-up": FeatureKey.REPORTS_FULL,
}

# In every plan, or not a hospital's at all (sign-in, the platform console).
# `/billing` and `/reports` are open as a whole and locked route by route
# above; `/reports/footfall` and `/reports/revenue` are open for today and
# check the plan themselves for any other period.
OPEN_PREFIXES: Final[tuple[str, ...]] = (
    "/auth",
    "/users",
    "/roles",
    "/audit-logs",
    "/invites",
    "/platform",
    "/hospitals",
    "/subscriptions",
    # Signup, password reset, contact: the caller has no hospital, so no plan.
    "/public",
    "/patients",
    "/departments",
    "/doctors",
    "/appointments",
    "/queue",
    "/search",
    "/encounters",
    "/orders",
    "/note-templates",
    "/billing",
    "/reports",
)
