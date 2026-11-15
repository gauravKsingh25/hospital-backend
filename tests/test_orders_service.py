"""UPI payment orders: what is owed, the proof, and when an order stops being payable."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.subscriptions import orders
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
    PaymentProof,
)
from tests.payments_support import PDF, PNG, chunks, pending_user, submitted_order

NEW = PaymentOrderPurpose.NEW


async def _refused(code: str, coroutine: object) -> DomainError:
    with pytest.raises(DomainError) as caught:
        await coroutine  # type: ignore[misc]
    assert caught.value.code == code, caught.value.code
    return caught.value


async def test_amount_comes_from_the_plan_table(session: AsyncSession) -> None:
    user = await pending_user(session, plan_code="professional")
    order = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    assert order.plan_code == "professional"
    assert order.amount_inr == 8999
    assert order.hospital_name == "City Care Clinic"
    assert order.status == PaymentOrderStatus.AWAITING_PAYMENT
    assert "am=8999.00" in order.upi_uri
    assert order.reference_code in order.upi_uri


async def test_reference_code_shape() -> None:
    for _ in range(200):
        assert re.fullmatch(r"NIR[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}", orders.new_reference_code())


async def test_same_plan_returns_the_live_order(session: AsyncSession) -> None:
    user = await pending_user(session)
    first = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    again = await orders.create_order(session, user=user, plan_code="standard", purpose=NEW)
    assert again.id == first.id


async def test_switching_plan_expires_the_old_order(session: AsyncSession) -> None:
    user = await pending_user(session)
    first = await orders.create_order(session, user=user, plan_code="standard", purpose=NEW)
    second = await orders.create_order(session, user=user, plan_code="professional", purpose=NEW)
    assert second.id != first.id
    assert second.amount_inr == 8999
    await session.refresh(first)
    assert first.status == PaymentOrderStatus.EXPIRED


async def test_submitted_order_blocks_a_new_one(session: AsyncSession) -> None:
    user = await pending_user(session)
    await submitted_order(session, user)
    await _refused(
        "order_under_review",
        orders.create_order(session, user=user, plan_code="professional", purpose=NEW),
    )


async def test_a_signed_up_user_cannot_renew(session: AsyncSession) -> None:
    # Renewal is for a hospital that exists; slice 3 enabled it for them only.
    user = await pending_user(session)
    await _refused(
        "tenant_required",
        orders.create_order(
            session, user=user, plan_code=None, purpose=PaymentOrderPurpose.RENEWAL
        ),
    )


async def test_hospital_staff_cannot_raise_a_new_order(
    session: AsyncSession, hospital_admin: User
) -> None:
    await _refused(
        "already_has_hospital",
        orders.create_order(session, user=hospital_admin, plan_code="standard", purpose=NEW),
    )


async def test_utr_on_another_order_is_taken(session: AsyncSession) -> None:
    await submitted_order(session, await pending_user(session), utr="111122223333")
    other = await pending_user(session)
    order = await orders.create_order(session, user=other, plan_code=None, purpose=NEW)
    await _refused(
        "utr_taken",
        orders.submit_payment(
            session, user=other, order_id=order.id, utr="111122223333", payer_upi_id=None
        ),
    )


async def test_a_submitted_order_cannot_be_submitted_again(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await submitted_order(session, user)
    await _refused(
        "order_not_payable",
        orders.submit_payment(
            session, user=user, order_id=order.id, utr="999988887777", payer_upi_id=None
        ),
    )


async def test_rejected_order_can_resubmit(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await submitted_order(session, user, utr="444455556666")
    # As `approval.reject` leaves it: rejected, its UTR released.
    order.status = PaymentOrderStatus.REJECTED
    order.rejection_reason = "No such payment"
    order.utr = None
    session.add(order)
    await session.commit()

    again = await orders.submit_payment(
        session, user=user, order_id=order.id, utr="444455556666", payer_upi_id=None
    )
    assert again.status == PaymentOrderStatus.SUBMITTED
    assert again.rejection_reason is None


async def test_expired_order_cannot_be_submitted(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    order.expires_at = utc_now() - timedelta(minutes=1)
    session.add(order)
    await session.commit()
    await _refused(
        "order_not_payable",
        orders.submit_payment(
            session, user=user, order_id=order.id, utr="123412341234", payer_upi_id=None
        ),
    )


async def test_another_users_order_is_not_found(session: AsyncSession) -> None:
    owner = await pending_user(session)
    order = await orders.create_order(session, user=owner, plan_code=None, purpose=NEW)
    stranger = await pending_user(session)
    await _refused(
        "order_not_found", orders.get_own_order(session, user=stranger, order_id=order.id)
    )


async def test_html_named_png_is_refused(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    error = await _refused(
        "proof_type_invalid",
        orders.attach_proof(
            session, user=user, order_id=order.id, body=chunks(b"<html><script>x</script>")
        ),
    )
    assert error.status_code == 422


async def test_oversize_stream_is_refused_and_nothing_stored(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    await session.commit()
    error = await _refused(
        "proof_too_large",
        orders.attach_proof(
            session, user=user, order_id=order.id, body=chunks(PNG + b"\x00" * (6 * 1024 * 1024))
        ),
    )
    assert error.status_code == 413
    await session.rollback()
    count = (
        await session.execute(sa.select(sa.func.count()).select_from(PaymentProof))
    ).scalar_one()
    assert count == 0


async def test_reupload_replaces_the_proof(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    await orders.attach_proof(session, user=user, order_id=order.id, body=chunks(PNG))
    await orders.attach_proof(session, user=user, order_id=order.id, body=chunks(PDF))
    proofs = (await session.execute(sa.select(PaymentProof))).scalars().all()
    assert len(proofs) == 1
    assert proofs[0].content_type == "application/pdf"
    assert proofs[0].size_bytes == len(PDF)


async def test_expire_unpaid_marks_old_orders(session: AsyncSession) -> None:
    user = await pending_user(session)
    order = await orders.create_order(session, user=user, plan_code=None, purpose=NEW)
    fresh_user = await pending_user(session)
    fresh = await orders.create_order(session, user=fresh_user, plan_code=None, purpose=NEW)
    order.expires_at = utc_now() - timedelta(minutes=1)
    session.add(order)
    await session.commit()

    assert await orders.expire_unpaid(session) == 1
    statuses = dict(
        (await session.execute(sa.select(PaymentOrder.id, PaymentOrder.status))).tuples().all()
    )
    assert statuses[order.id] == PaymentOrderStatus.EXPIRED
    assert statuses[fresh.id] == PaymentOrderStatus.AWAITING_PAYMENT
