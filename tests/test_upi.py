"""The UPI link a payer's app opens, and its QR code."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from app.modules.subscriptions import upi


def test_uri_encodes_payee_and_amount() -> None:
    uri = upi.build_uri(
        vpa="nirogya@okaxis", payee="Nirogya Health", amount_inr=3000, reference="NIR7K2Q9M"
    )
    assert uri.startswith("upi://pay?")
    assert "pn=Nirogya%20Health" in uri
    assert "+" not in uri
    params = {k: v[0] for k, v in parse_qs(urlsplit(uri).query).items()}
    assert params["pa"] == "nirogya@okaxis"
    assert params["am"] == "3000.00"
    assert params["cu"] == "INR"
    assert params["tr"] == "NIR7K2Q9M"
    assert "NIR7K2Q9M" in params["tn"]


def test_qr_svg_is_a_standalone_svg() -> None:
    svg = upi.qr_svg("upi://pay?pa=a@b&am=1.00")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert svg.rstrip().endswith("</svg>")
