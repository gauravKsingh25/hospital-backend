"""Permissions enforced by `subscriptions`.

Seeing the plan is for whoever runs the hospital; changing it is the same
person today, kept apart so a future "billing contact" can be given one
without the other. The platform permission is the owner's: extending or
overriding a hospital's subscription by hand.
"""

from __future__ import annotations

from typing import Final

from app.modules.identity.rbac import PermissionDef, Roles

MODULE: Final[str] = "subscriptions"


class SubscriptionPermissions:
    SUBSCRIPTION_READ = "subscription:read"
    SUBSCRIPTION_MANAGE = "subscription:manage"
    PLATFORM_SUBSCRIPTION_MANAGE = "platform:subscription_manage"
    # The UPI payment queue: seeing payments (and their screenshots), and
    # deciding them. Support staff may look; only the owner approves.
    PLATFORM_PAYMENT_READ = "platform:payment_read"
    PLATFORM_PAYMENT_REVIEW = "platform:payment_review"


PERMISSION_CATALOGUE: Final[tuple[PermissionDef, ...]] = (
    PermissionDef(
        SubscriptionPermissions.SUBSCRIPTION_READ,
        MODULE,
        "View the hospital's plan, status and renewal date.",
    ),
    PermissionDef(
        SubscriptionPermissions.SUBSCRIPTION_MANAGE,
        MODULE,
        "Renew, upgrade, downgrade or cancel the hospital's plan.",
    ),
    PermissionDef(
        SubscriptionPermissions.PLATFORM_SUBSCRIPTION_MANAGE,
        MODULE,
        "Extend or override any hospital's subscription.",
    ),
    PermissionDef(
        SubscriptionPermissions.PLATFORM_PAYMENT_READ,
        MODULE,
        "View payments awaiting review and their proof.",
    ),
    PermissionDef(
        SubscriptionPermissions.PLATFORM_PAYMENT_REVIEW,
        MODULE,
        "Approve or reject a hospital's payment.",
    ),
)

MODULE_ROLE_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    Roles.PLATFORM_ADMIN: tuple(perm.code for perm in PERMISSION_CATALOGUE),
    Roles.HOSPITAL_ADMIN: (
        SubscriptionPermissions.SUBSCRIPTION_READ,
        SubscriptionPermissions.SUBSCRIPTION_MANAGE,
    ),
    Roles.PLATFORM_SUPPORT: (SubscriptionPermissions.PLATFORM_PAYMENT_READ,),
}
