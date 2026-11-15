"""Shared helpers for the signup-payment tests: a signed-up, unpaid user."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.security import hash_password
from app.modules.identity.models import SignupIntent, SignupIntentStatus, User
from app.modules.subscriptions import orders
from app.modules.subscriptions.models import PaymentOrder, PaymentOrderPurpose
from tests.conftest import TEST_PASSWORD

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64


async def chunks(data: bytes, size: int = 64 * 1024) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def pending_user(
    session: AsyncSession,
    *,
    plan_code: str = "standard",
    hospital_name: str = "City Care Clinic",
    email: str | None = None,
) -> User:
    """Someone who has signed up but not paid: no hospital, no roles."""
    address = email or f"{uuid.uuid4().hex[:10]}@signup.example"
    async with db.system_context(session):
        user = User(
            hospital_id=None,
            email=address,
            full_name="Asha Verma",
            phone="9876543210",
            password_hash=hash_password(TEST_PASSWORD),
            must_change_password=False,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        session.add(
            SignupIntent(
                email=address,
                full_name=user.full_name,
                hospital_name=hospital_name,
                plan_code=plan_code,
                status=SignupIntentStatus.COMPLETED,
                user_id=user.id,
            )
        )
        await session.flush()
        await session.refresh(user)
    await session.commit()
    return user


async def submitted_order(
    session: AsyncSession, user: User, *, utr: str = "123456789012", plan_code: str | None = None
) -> PaymentOrder:
    """An order with its UTR in, waiting for the platform owner."""
    order = await orders.create_order(
        session, user=user, plan_code=plan_code, purpose=PaymentOrderPurpose.NEW
    )
    await orders.submit_payment(
        session, user=user, order_id=order.id, utr=utr, payer_upi_id="asha@okhdfc"
    )
    await session.commit()
    return order
