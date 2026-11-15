"""The platform console's payment queue and contact inbox. Thin.

Reading needs `platform:payment_read` (the owner and support staff);
deciding needs `platform:payment_review` (the owner). The screenshot is
served only here, with the stored type, `nosniff`, and PDFs as downloads.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.core.deps import SessionDep, require
from app.core.models import utc_now
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import signup
from app.modules.identity.rbac import Permissions
from app.modules.identity.service import AuthContext, get_user_contacts
from app.modules.subscriptions import approval, emails, lifecycle, queue
from app.modules.subscriptions.models import PaymentOrder, PaymentOrderStatus, Subscription
from app.modules.subscriptions.rbac import SubscriptionPermissions
from app.modules.subscriptions.schemas import (
    ContactMessageRead,
    EmailOutcome,
    PaymentOrderRead,
    PaymentReject,
    PlatformPaymentRow,
    PlatformSubscriptionRow,
    SubscriptionDetail,
    SubscriptionExtend,
)
from app.modules.tenancy import service as tenancy_service

payments_router = APIRouter(prefix="/platform", tags=["platform"])

CanReadPayments = Annotated[
    AuthContext, Depends(require(SubscriptionPermissions.PLATFORM_PAYMENT_READ))
]
CanReviewPayments = Annotated[
    AuthContext, Depends(require(SubscriptionPermissions.PLATFORM_PAYMENT_REVIEW))
]


async def _rows(session: SessionDep, orders: list[PaymentOrder]) -> list[PlatformPaymentRow]:
    ids = [order.id for order in orders]
    payers = await get_user_contacts(session, {order.user_id for order in orders})
    proofs = await queue.proofs_for(session, ids)
    receipts = await queue.receipt_numbers_for(session, ids)
    rows = []
    for order in orders:
        name, email = payers.get(order.user_id, (None, None))
        rows.append(
            PlatformPaymentRow(
                **PaymentOrderRead.from_order(order).model_dump(),
                payer_name=name,
                payer_email=email,
                payer_upi_id=order.payer_upi_id,
                hospital_name=order.hospital_name,
                hospital_id=order.hospital_id,
                reviewed_at=order.reviewed_at,
                has_proof=order.id in proofs,
                receipt_number=receipts.get(order.id),
            )
        )
    return rows


async def _row(session: SessionDep, order_id: uuid.UUID) -> PlatformPaymentRow:
    [row] = await _rows(session, [await queue.get_payment(session, order_id)])
    return row


@payments_router.get(
    "/payments", response_model=Page[PlatformPaymentRow], summary="Payments, newest first"
)
async def list_payments(
    session: SessionDep,
    _: CanReadPayments,
    params: Annotated[PageParams, Depends(page_params)],
    status: PaymentOrderStatus | None = None,
) -> Page[PlatformPaymentRow]:
    orders, total = await queue.list_payments(session, status=status, params=params)
    return Page.build(await _rows(session, orders), total=total, params=params)


@payments_router.get(
    "/payments/{order_id}", response_model=PlatformPaymentRow, summary="One payment"
)
async def get_payment(
    order_id: uuid.UUID, session: SessionDep, _: CanReadPayments
) -> PlatformPaymentRow:
    return await _row(session, order_id)


@payments_router.get(
    "/payments/{order_id}/proof",
    summary="The payer's screenshot",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "application/pdf": {}}}},
)
async def get_proof(order_id: uuid.UUID, session: SessionDep, _: CanReadPayments) -> Response:
    proof = await queue.get_proof(session, order_id)
    disposition = "attachment" if proof.content_type == "application/pdf" else "inline"
    extension = {"image/png": "png", "image/jpeg": "jpg"}.get(proof.content_type, "pdf")
    return Response(
        content=proof.data,
        media_type=proof.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="payment-proof.{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@payments_router.post(
    "/payments/{order_id}/approve",
    response_model=PlatformPaymentRow,
    summary="Approve: create the hospital, its admin, subscription and receipt",
)
async def approve_payment(
    order_id: uuid.UUID, session: SessionDep, context: CanReviewPayments
) -> PlatformPaymentRow:
    order, approved_now = await approval.approve_once(
        session, order_id=order_id, actor=context.user
    )
    await session.commit()
    if approved_now:
        # After the commit: the hospital stands whatever the mail server does.
        await emails.send_receipt(session, order_id=order.id, sent_by=context.user)
        await session.commit()
    return await _row(session, order.id)


@payments_router.post(
    "/payments/{order_id}/reject",
    response_model=PlatformPaymentRow,
    summary="Reject with a reason the payer will read",
)
async def reject_payment(
    order_id: uuid.UUID, payload: PaymentReject, session: SessionDep, context: CanReviewPayments
) -> PlatformPaymentRow:
    order = await approval.reject(
        session, order_id=order_id, actor=context.user, reason=payload.reason
    )
    await session.commit()
    await emails.send_rejected_for(session, order_id=order.id, sent_by=context.user)
    await session.commit()
    return await _row(session, order.id)


@payments_router.post(
    "/payments/{order_id}/resend-receipt",
    response_model=EmailOutcome,
    summary="Email the approval and receipt again",
)
async def resend_receipt(
    order_id: uuid.UUID, session: SessionDep, context: CanReviewPayments
) -> EmailOutcome:
    email = await emails.send_receipt(session, order_id=order_id, sent_by=context.user)
    await session.commit()
    return EmailOutcome(status=email.status.value, error=email.error)


@payments_router.get(
    "/contact-messages",
    response_model=Page[ContactMessageRead],
    summary="Messages from the website's contact form",
)
async def list_contact_messages(
    session: SessionDep,
    _: CanReadPayments,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[ContactMessageRead]:
    rows, total = await signup.list_contact_messages(session, params)
    return Page.build(
        [ContactMessageRead.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        params=params,
    )


CanReadHospitals = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_HOSPITAL_READ))]
CanManageSubscriptions = Annotated[
    AuthContext, Depends(require(SubscriptionPermissions.PLATFORM_SUBSCRIPTION_MANAGE))
]


async def _subscription_rows(
    session: SessionDep, subscriptions: list[Subscription]
) -> list[PlatformSubscriptionRow]:
    names = await tenancy_service.get_hospital_names(
        session, {row.hospital_id for row in subscriptions}
    )
    now = utc_now()
    return [
        PlatformSubscriptionRow(
            **SubscriptionDetail.from_subscription(row, now=now).model_dump(),
            id=row.id,
            hospital_id=row.hospital_id,
            hospital_name=names.get(row.hospital_id),
        )
        for row in subscriptions
    ]


@payments_router.get(
    "/subscriptions",
    response_model=Page[PlatformSubscriptionRow],
    summary="Every hospital's subscription, the soonest to end first",
)
async def list_subscriptions(
    session: SessionDep,
    _: CanReadHospitals,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[PlatformSubscriptionRow]:
    rows, total = await lifecycle.list_subscriptions(session, params=params)
    return Page.build(await _subscription_rows(session, rows), total=total, params=params)


@payments_router.post(
    "/subscriptions/{subscription_id}/extend",
    response_model=PlatformSubscriptionRow,
    summary="Add days to a hospital's paid period",
)
async def extend_subscription(
    subscription_id: uuid.UUID,
    payload: SubscriptionExtend,
    session: SessionDep,
    context: CanManageSubscriptions,
) -> PlatformSubscriptionRow:
    subscription = await lifecycle.extend(
        session,
        subscription_id=subscription_id,
        days=payload.days,
        reason=payload.reason,
        actor=context.user,
    )
    await session.commit()
    [row] = await _subscription_rows(session, [subscription])
    return row
