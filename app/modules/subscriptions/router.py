"""Subscription routes for a hospital's own staff, and for paying. Thin.

Mounted under `/subscriptions`, which stays reachable when the subscription
has lapsed (see `core.deps`): a locked-out hospital must be able to see why.

Raising and paying an order belongs to someone who has signed up and not yet
paid: an account with no hospital and no role. They need no permission (such
a person holds none); `pending_user` admits exactly them. Reading is open to
any signed-in owner, because the payment screen is still polling its order
when the approval makes that person a hospital administrator. Every order is
looked up by its owner.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import settings
from app.core.deps import CurrentUser, SessionDep, TenantId, require
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.models import utc_now
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity.service import AuthContext, require_permission
from app.modules.subscriptions import approval, emails, lifecycle, orders, upi
from app.modules.subscriptions import service as subscriptions_service
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
)
from app.modules.subscriptions.rbac import SubscriptionPermissions
from app.modules.subscriptions.schemas import (
    EntitlementsRead,
    PaymentOrderCreate,
    PaymentOrderDetail,
    PaymentOrderRead,
    PaymentSubmit,
    PlanChange,
    SubscriptionDetail,
    UpgradeQuote,
)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

CanRead = Annotated[AuthContext, Depends(require(SubscriptionPermissions.SUBSCRIPTION_READ))]
CanManage = Annotated[AuthContext, Depends(require(SubscriptionPermissions.SUBSCRIPTION_MANAGE))]


async def pending_user(context: CurrentUser) -> AuthContext:
    """Signed up, not yet approved: no hospital, and not platform staff."""
    if context.hospital_id is not None or context.is_platform_user:
        raise PermissionDeniedError(
            "Your account already belongs to a hospital.", code="already_has_hospital"
        )
    return context


def _may_manage(context: AuthContext) -> bool:
    """May pay any of their hospital's renewal or upgrade orders, not just their own."""
    return context.hospital_id is not None and context.has_permission(
        SubscriptionPermissions.SUBSCRIPTION_MANAGE
    )


@router.get("/current", response_model=EntitlementsRead, summary="The hospital's subscription")
async def current(context: CanRead, _: TenantId) -> EntitlementsRead:
    # Already resolved on the auth context that authenticated this request.
    if context.entitlements is None:
        raise NotFoundError("This hospital has no subscription.", code="subscription_not_found")
    return EntitlementsRead.from_entitlements(context.entitlements)


async def _detail(session: SessionDep, order: PaymentOrder) -> PaymentOrderDetail:
    receipt = (
        await approval.get_receipt(session, order.id)
        if order.status == PaymentOrderStatus.APPROVED
        else None
    )
    return PaymentOrderDetail(
        **PaymentOrderRead.from_order(order).model_dump(),
        upi_uri=order.upi_uri,
        upi_vpa=order.upi_vpa,
        payee_name=order.payee_name,
        qr_svg=upi.qr_svg(order.upi_uri),
        review_hours=settings.PAYMENT_REVIEW_HOURS,
        has_proof=await orders.has_proof(session, order.id),
        receipt_number=receipt.receipt_number if receipt else None,
    )


@router.post(
    "/orders",
    response_model=PaymentOrderRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start paying for a plan (or get the payment already started)",
)
async def create_order(
    payload: PaymentOrderCreate, session: SessionDep, context: CurrentUser
) -> PaymentOrderRead:
    if payload.purpose == PaymentOrderPurpose.NEW:
        # A new hospital: only someone signed up and not yet approved.
        await pending_user(context)
    else:
        # Renewing or upgrading a hospital: its subscription manager. Reached
        # even when the subscription has lapsed (`/subscriptions` is open).
        require_permission(context, SubscriptionPermissions.SUBSCRIPTION_MANAGE)
        if context.hospital_id is None:
            raise PermissionDeniedError(
                "This action requires a hospital context.", code="tenant_required"
            )
    order = await orders.create_order(
        session, user=context.user, plan_code=payload.plan_code, purpose=payload.purpose
    )
    await session.commit()
    return PaymentOrderRead.from_order(order)


@router.get("/orders", response_model=Page[PaymentOrderRead], summary="My payments, newest first")
async def list_orders(
    session: SessionDep,
    # Not only pending users: the payment screen keeps reading its order
    # after the approval that gave its owner a hospital. Owner-scoped.
    context: CurrentUser,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[PaymentOrderRead]:
    rows, total = await orders.list_own_orders(session, user=context.user, params=params)
    return Page.build(
        [PaymentOrderRead.from_order(row) for row in rows], total=total, params=params
    )


@router.get(
    "/orders/{order_id}", response_model=PaymentOrderDetail, summary="One payment, with its QR"
)
async def get_order(
    order_id: uuid.UUID, session: SessionDep, context: CurrentUser
) -> PaymentOrderDetail:
    order = await orders.get_payable_order(
        session, user=context.user, order_id=order_id, may_manage=_may_manage(context)
    )
    return await _detail(session, order)


@router.put(
    "/orders/{order_id}/proof",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upload a screenshot of the payment (PNG, JPEG or PDF, up to 5 MB)",
)
async def upload_proof(
    order_id: uuid.UUID, request: Request, session: SessionDep, context: CurrentUser
) -> Response:
    # The raw body, read as a stream: no multipart parser, and the size limit
    # holds whatever Content-Length claims.
    await orders.attach_proof(
        session,
        user=context.user,
        order_id=order_id,
        body=request.stream(),
        may_manage=_may_manage(context),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/orders/{order_id}/submit",
    response_model=PaymentOrderRead,
    summary="Send the UTR for checking",
)
async def submit_payment(
    order_id: uuid.UUID, payload: PaymentSubmit, session: SessionDep, context: CurrentUser
) -> PaymentOrderRead:
    order = await orders.submit_payment(
        session,
        user=context.user,
        order_id=order_id,
        utr=payload.utr,
        payer_upi_id=payload.payer_upi_id,
        may_manage=_may_manage(context),
    )
    await session.commit()
    # After the commit: the submission stands whatever the mail server does.
    await emails.send_submitted(session, order=order, payer=context.user)
    await session.commit()
    return PaymentOrderRead.from_order(order)


@router.get(
    "/receipts/{order_id}/pdf",
    summary="Download the receipt for an approved payment",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def download_receipt(
    order_id: uuid.UUID, session: SessionDep, context: CurrentUser
) -> Response:
    order = await orders.get_order_for_receipt(
        session,
        order_id=order_id,
        user_id=context.user.id,
        hospital_id=context.hospital_id,
        may_manage=context.has_permission(SubscriptionPermissions.SUBSCRIPTION_MANAGE),
    )
    receipt = (
        await approval.get_receipt(session, order.id)
        if order.status == PaymentOrderStatus.APPROVED
        else None
    )
    if receipt is None:
        raise NotFoundError("There is no receipt for this payment.", code="receipt_not_found")
    return Response(
        content=receipt.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{receipt.receipt_number}.pdf"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/history", response_model=Page[PaymentOrderRead], summary="The hospital's payments")
async def history(
    session: SessionDep,
    _: CanRead,
    hospital_id: TenantId,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[PaymentOrderRead]:
    rows, total = await orders.list_hospital_orders(session, hospital_id=hospital_id, params=params)
    return Page.build(
        [PaymentOrderRead.from_order(row) for row in rows], total=total, params=params
    )


@router.get(
    "/detail", response_model=SubscriptionDetail, summary="The subscription, for managing it"
)
async def detail(session: SessionDep, _: CanRead, hospital_id: TenantId) -> SubscriptionDetail:
    subscription = await subscriptions_service.get_subscription(session, hospital_id)
    if subscription is None:
        raise NotFoundError("This hospital has no subscription.", code="subscription_not_found")
    return SubscriptionDetail.from_subscription(subscription, now=utc_now())


@router.get("/upgrade-quote", response_model=UpgradeQuote, summary="What an upgrade would cost now")
async def upgrade_quote(
    plan_code: str, session: SessionDep, _: CanManage, hospital_id: TenantId
) -> UpgradeQuote:
    amount, period_end = await orders.upgrade_quote(
        session, hospital_id=hospital_id, plan_code=plan_code
    )
    return UpgradeQuote(plan_code=plan_code, amount_inr=amount, period_end=period_end)


@router.post(
    "/change-plan",
    response_model=SubscriptionDetail,
    summary="Move to a lower plan at the next renewal (or undo that)",
)
async def change_plan(
    payload: PlanChange, session: SessionDep, context: CanManage, hospital_id: TenantId
) -> SubscriptionDetail:
    subscription = await lifecycle.schedule_change(
        session, hospital_id=hospital_id, plan_code=payload.plan_code, actor=context.user
    )
    await session.commit()
    return SubscriptionDetail.from_subscription(subscription, now=utc_now())


@router.post("/cancel", response_model=SubscriptionDetail, summary="Stop at the end of the month")
async def cancel(
    session: SessionDep, context: CanManage, hospital_id: TenantId
) -> SubscriptionDetail:
    subscription = await lifecycle.cancel(session, hospital_id=hospital_id, actor=context.user)
    await session.commit()
    return SubscriptionDetail.from_subscription(subscription, now=utc_now())


@router.post("/resume", response_model=SubscriptionDetail, summary="Undo a cancellation")
async def resume(
    session: SessionDep, context: CanManage, hospital_id: TenantId
) -> SubscriptionDetail:
    subscription = await lifecycle.resume(session, hospital_id=hospital_id, actor=context.user)
    await session.commit()
    return SubscriptionDetail.from_subscription(subscription, now=utc_now())
