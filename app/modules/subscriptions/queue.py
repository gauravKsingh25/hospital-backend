"""The platform owner's view of payments: the review queue and its evidence.

Read-only, cross-tenant (a new signup's order belongs to no hospital), and
reached only through `/platform/payments`, whose routes require the platform
payment permissions. The screenshot is served from here and nowhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.database import system_context
from app.core.exceptions import NotFoundError
from app.core.pagination import PageParams
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentProof,
    PaymentReceipt,
)

__all__ = ["get_payment", "get_proof", "list_payments", "proofs_for", "receipt_numbers_for"]


async def list_payments(
    session: AsyncSession, *, status: PaymentOrderStatus | None, params: PageParams
) -> tuple[list[PaymentOrder], int]:
    """Newest submission first; orders never submitted sort last."""
    scope = [col(PaymentOrder.status) == status] if status is not None else []
    async with system_context(session):
        total = (
            await session.execute(select(func.count()).select_from(PaymentOrder).where(*scope))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(PaymentOrder)
                    .where(*scope)
                    .order_by(
                        col(PaymentOrder.submitted_at).desc().nulls_last(),
                        col(PaymentOrder.created_at).desc(),
                    )
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
    return list(rows), int(total)


async def get_payment(session: AsyncSession, order_id: uuid.UUID) -> PaymentOrder:
    async with system_context(session):
        order = await session.get(PaymentOrder, order_id)
    if order is None:
        raise NotFoundError("Payment not found.", code="order_not_found")
    return order


async def proofs_for(session: AsyncSession, order_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these orders have a screenshot. Never loads the bytes."""
    if not order_ids:
        return set()
    async with system_context(session):
        rows = await session.execute(
            select(col(PaymentProof.payment_order_id)).where(
                col(PaymentProof.payment_order_id).in_(set(order_ids))
            )
        )
        return set(rows.scalars().all())


async def receipt_numbers_for(
    session: AsyncSession, order_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not order_ids:
        return {}
    async with system_context(session):
        rows = await session.execute(
            select(col(PaymentReceipt.payment_order_id), col(PaymentReceipt.receipt_number)).where(
                col(PaymentReceipt.payment_order_id).in_(set(order_ids))
            )
        )
        return {row[0]: row[1] for row in rows.all()}


async def get_proof(session: AsyncSession, order_id: uuid.UUID) -> PaymentProof:
    async with system_context(session):
        proof: PaymentProof | None = (
            (
                await session.execute(
                    select(PaymentProof).where(col(PaymentProof.payment_order_id) == order_id)
                )
            )
            .scalars()
            .first()
        )
    if proof is None:
        raise NotFoundError("No screenshot was uploaded for this payment.", code="proof_not_found")
    return proof
