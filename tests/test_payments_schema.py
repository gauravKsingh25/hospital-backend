"""The signup and payment tables as the migration builds them.

Checked against the migrated database rather than the models: the guarantees
that matter here — one UTR per order, one screenshot per order, one receipt
per order, a receipt sequence — are database constraints, and a model that
declares them proves nothing about the schema that ships.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def _indexes(session: AsyncSession, table: str) -> dict[str, str]:
    rows = await session.execute(
        sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"), {"t": table}
    )
    return {str(name): str(definition) for name, definition in rows.tuples()}


async def test_signup_and_payment_tables_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        sa.text("SELECT table_name FROM information_schema.tables WHERE table_name = ANY(:names)"),
        {
            "names": [
                "email_otps",
                "signup_intents",
                "contact_messages",
                "payment_orders",
                "payment_proofs",
                "payment_receipts",
            ]
        },
    )
    assert {name for (name,) in rows} == {
        "email_otps",
        "signup_intents",
        "contact_messages",
        "payment_orders",
        "payment_proofs",
        "payment_receipts",
    }


async def test_a_utr_can_be_on_only_one_order(session: AsyncSession) -> None:
    definitions = (await _indexes(session, "payment_orders")).values()
    utr = [d for d in definitions if "(utr)" in d]
    assert len(utr) == 1
    assert "UNIQUE" in utr[0]
    assert "WHERE (utr IS NOT NULL)" in utr[0]


async def test_reference_code_is_unique(session: AsyncSession) -> None:
    definitions = (await _indexes(session, "payment_orders")).values()
    assert any("UNIQUE" in d and "(reference_code)" in d for d in definitions)


async def test_one_proof_and_one_receipt_per_order(session: AsyncSession) -> None:
    for table in ("payment_proofs", "payment_receipts"):
        definitions = (await _indexes(session, table)).values()
        assert any("UNIQUE" in d and "(payment_order_id)" in d for d in definitions), table


async def test_receipt_numbers_come_from_a_sequence(session: AsyncSession) -> None:
    first = (await session.execute(sa.text("SELECT nextval('payment_receipt_seq')"))).scalar_one()
    second = (await session.execute(sa.text("SELECT nextval('payment_receipt_seq')"))).scalar_one()
    assert second == first + 1


async def test_new_platform_email_kinds_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        sa.text("SELECT unnest(enum_range(NULL::platformemailkind))::text")
    )
    kinds = {kind for (kind,) in rows}
    assert {
        "OTP",
        "SIGNUP_EXISTS",
        "PASSWORD_RESET",
        "PAYMENT_SUBMITTED",
        "PAYMENT_ALERT",
        "PAYMENT_APPROVED",
        "PAYMENT_REJECTED",
        "CONTACT_ALERT",
    } <= kinds
