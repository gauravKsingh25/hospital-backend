"""FastAPI dependencies that lock a route to a plan feature.

Kept apart from `features.py`, which `service.py` imports: this file needs
`core.deps`, and `core.deps` reaches back through `identity` to the service.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.exceptions import FeatureNotInPlanError
from app.modules.identity.service import AuthContext
from app.modules.subscriptions.features import FeatureKey

__all__ = ["check_feature", "require_feature"]


def check_feature(context: AuthContext, feature: FeatureKey) -> None:
    """Raise unless the caller's hospital has `feature`. Platform users pass."""
    if not settings.SUBSCRIPTIONS_ENFORCED or context.is_platform_user:
        return
    entitlements = context.entitlements
    if entitlements is None or not entitlements.has(feature.value):
        raise FeatureNotInPlanError(
            details={
                "feature": feature.value,
                "plan": entitlements.plan_code if entitlements else None,
            }
        )


def require_feature(feature: FeatureKey) -> Callable[[AuthContext], Awaitable[AuthContext]]:
    """Dependency factory: 403 `feature_not_in_plan` without `feature`.

        app.include_router(wards_router, dependencies=[Depends(require_feature(FeatureKey.IPD))])

    Sits beside `require(...)`, never instead of it: the plan says what the
    hospital bought, the permission says what this person may do with it.
    """

    async def dependency(context: CurrentUser) -> AuthContext:
        check_feature(context, feature)
        return context

    # Read by the test that proves every gated route really is gated.
    dependency.feature = feature  # type: ignore[attr-defined]
    return dependency
