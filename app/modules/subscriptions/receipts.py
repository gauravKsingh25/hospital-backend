"""The receipt for an approved subscription payment, as a one-page PDF.

Drawn with reportlab's canvas: one fixed layout, nothing that needs a
template engine. Prices are GST-inclusive and the receipt says so; there is
no GST breakdown (spec §2 non-goals). The PDF is stored as issued, so a
receipt downloaded next year is the one that was emailed.
"""

from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

__all__ = ["render_pdf"]

_IST = ZoneInfo("Asia/Kolkata")


def _date(moment: datetime) -> str:
    local = moment.astimezone(_IST)
    return f"{local.day} {local:%b %Y}"


def _rupees(amount: int) -> str:
    # The built-in PDF fonts have no ₹ glyph; "Rs." is what Indian receipts
    # printed without it have always said.
    return f"Rs. {amount:,}.00"


def render_pdf(
    *,
    receipt_number: str,
    issued_at: datetime,
    hospital_name: str,
    payer_name: str,
    payer_email: str,
    plan_name: str,
    amount_inr: int,
    reference_code: str,
    utr: str,
    period_start: datetime,
    period_end: datetime,
    seller_name: str,
    seller_contact: str,
) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Receipt {receipt_number}")
    pdf.setAuthor(seller_name)
    width, height = A4
    left, right = 20 * mm, width - 20 * mm
    y = height - 25 * mm

    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(left, y, seller_name)
    pdf.setFont("Helvetica", 10)
    pdf.drawRightString(right, y, "PAYMENT RECEIPT")
    y -= 6 * mm
    pdf.drawString(left, y, seller_contact)

    y -= 14 * mm
    rows = (
        ("Receipt number", receipt_number),
        ("Date", _date(issued_at)),
        ("Received from", f"{payer_name} <{payer_email}>"),
        ("Hospital", hospital_name),
        ("Payment reference", reference_code),
        ("UPI transaction (UTR)", utr),
        ("Paid by", "UPI"),
    )
    for label, value in rows:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(left, y, label)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left + 55 * mm, y, value)
        y -= 7 * mm

    y -= 6 * mm
    pdf.line(left, y, right, y)
    y -= 8 * mm
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left, y, "Description")
    pdf.drawRightString(right, y, "Amount")
    y -= 8 * mm
    pdf.setFont("Helvetica", 10)
    pdf.drawString(
        left,
        y,
        f"Nirogya {plan_name} plan, {_date(period_start)} to {_date(period_end)}",
    )
    pdf.drawRightString(right, y, _rupees(amount_inr))
    y -= 6 * mm
    pdf.line(left, y, right, y)
    y -= 9 * mm
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(left, y, "Total paid")
    pdf.drawRightString(right, y, _rupees(amount_inr))
    y -= 7 * mm
    pdf.setFont("Helvetica", 9)
    pdf.drawString(left, y, "Amount includes GST.")

    pdf.setFont("Helvetica", 8)
    pdf.drawString(
        left, 20 * mm, "This receipt was generated electronically and needs no signature."
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
