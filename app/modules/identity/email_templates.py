"""The platform's own emails, rendered.

Plain functions over string templates: there are two emails, both English, and
a template engine would be more machinery than message. The HTML is a single
table-based block with inline styles because that is what survives Gmail,
Outlook and a phone's mail app alike; there are no images to be blocked.
"""

# Email HTML carries its styles inline, one element per line; wrapping those
# lines would put whitespace inside attributes that some clients render.
# ruff: noqa: E501

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

__all__ = ["format_expiry", "render_invite"]

_IST = ZoneInfo("Asia/Kolkata")


def format_expiry(moment: datetime) -> str:
    """`3 Nov 2026, 2:15 pm IST` — how a person in India reads a deadline."""
    local = moment.astimezone(_IST)
    hour = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    return f"{local.day} {local:%b %Y}, {hour}:{local:%M} {suffix} IST"


def render_invite(
    *,
    full_name: str,
    hospital_name: str | None,
    role_label: str,
    url: str,
    expires_at: datetime,
    support_contact: str,
) -> tuple[str, str, str]:
    """Return (subject, text, html) for an invitation."""
    expiry = format_expiry(expires_at)
    if hospital_name:
        subject = f"You're invited to manage {hospital_name}"
        purpose = f"You've been invited to set up and manage {hospital_name} as its {role_label}."
    else:
        subject = "You're invited to the Hospital Management platform team"
        purpose = f"You've been invited to join the platform team as {role_label}."

    text = (
        f"Hello {full_name},\n\n"
        f"{purpose}\n\n"
        f"Set up your account and choose your password here:\n{url}\n\n"
        f"This link works once and expires on {expiry}.\n"
        f"If you weren't expecting this, you can ignore this email, or contact "
        f"{support_contact}.\n"
    )

    html = f"""\
<!doctype html>
<html><body style="margin:0;padding:24px;background:#f4f5f7;font-family:Arial,Helvetica,sans-serif;color:#111827">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px">
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.5">
<p style="margin:0 0 16px">Hello {escape(full_name)},</p>
<p style="margin:0 0 24px">{escape(purpose)}</p>
<p style="margin:0 0 24px"><a href="{escape(url, quote=True)}" style="display:inline-block;background:#1d4ed8;color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 22px;border-radius:8px">Set up your account</a></p>
<p style="margin:0 0 8px;font-size:14px;color:#4b5563">This link works once and expires on {escape(expiry)}.</p>
<p style="margin:0 0 24px;font-size:14px;color:#4b5563">If the button doesn't work, copy this address into your browser:<br><span style="word-break:break-all">{escape(url)}</span></p>
</td></tr>
<tr><td style="padding:16px 28px 24px;border-top:1px solid #e5e7eb;font-size:13px;color:#6b7280">
Weren't expecting this? You can ignore this email, or contact {escape(support_contact)}.
</td></tr>
</table>
</td></tr>
</table>
</body></html>
"""
    return subject, text, html
