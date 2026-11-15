"""The UPI payment link and its QR code.

A `upi://pay` URI is what every UPI app in India understands, whether opened
as a link on a phone or scanned as a QR from a laptop screen. The reference
code goes in both `tn` (the note the payer sees) and `tr` (the transaction
reference), so it shows up next to the payment in the owner's bank app —
which is how a payment is matched to its order by eye.
"""

from __future__ import annotations

import io
from urllib.parse import quote

import segno

__all__ = ["build_uri", "qr_svg"]


def build_uri(*, vpa: str, payee: str, amount_inr: int, reference: str) -> str:
    """`upi://pay?pa=…&pn=…&am=3000.00&cu=INR&tn=…&tr=…`."""
    params = (
        ("pa", vpa),
        ("pn", payee),
        ("am", f"{amount_inr}.00"),
        ("cu", "INR"),
        ("tn", f"Nirogya {reference}"),
        ("tr", reference),
    )
    # `quote`, not `urlencode`: several UPI apps read "+" literally rather
    # than as a space, and show the payee as "Nirogya+Health".
    return "upi://pay?" + "&".join(f"{key}={quote(value, safe='@.')}" for key, value in params)


def qr_svg(uri: str) -> str:
    """The URI as a standalone SVG QR code, sized for a screen and a camera.

    With its namespace, so the app can show it as an image (`data:` URL)
    rather than injecting markup into the page.
    """
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(
        buffer,
        kind="svg",
        xmldecl=False,
        svgns=True,
        nl=False,
        scale=6,
        border=2,
        svgclass=None,
        lineclass=None,
    )
    return buffer.getvalue().decode("utf-8")
