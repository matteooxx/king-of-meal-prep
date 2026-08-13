"""Email notifications: password reset + (future) weekly digest.

Same SMTP plumbing as nas-share-ui / recsbot-ui — Gmail app password from
app.env. Field-journal styling in the HTML so the email looks like the app.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as _html_escape

import settings as app_settings

log = logging.getLogger("king-of-meal-prep")


def _safe_subject(text: str) -> str:
    return text.replace("\r", "").replace("\n", "")


def _smtp_configured() -> bool:
    return bool(
        app_settings.get("SMTP_USER")
        and app_settings.get("SMTP_PASS")
        and app_settings.get("SMTP_FROM")
    )


def send_password_reset_email(
    recipient: str, reset_link: str, ttl_minutes: int, requester_ip: str = ""
) -> None:
    if not (_smtp_configured() and recipient):
        log.error("password reset email skipped: SMTP not configured")
        return
    try:
        smtp_host = app_settings.get("SMTP_HOST") or "smtp.gmail.com"
        try:
            smtp_port = int(app_settings.get("SMTP_PORT") or 587)
        except (TypeError, ValueError):
            raise ValueError("SMTP_PORT must be an integer")
        smtp_user = app_settings.get("SMTP_USER")
        smtp_pass = app_settings.get("SMTP_PASS")
        smtp_from = app_settings.get("SMTP_FROM")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = _safe_subject("KING — Reset password")
        msg["From"] = smtp_from
        msg["To"] = recipient
        safe_link = _html_escape(reset_link)
        safe_ip = _html_escape(requester_ip or "?")
        when = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
        html = (
            '<html><body style="font-family:-apple-system,sans-serif;background:#0a0a0a;color:#e8e6e0;padding:2rem;">'
            '<div style="max-width:500px;margin:0 auto;background:#131313;border:1px dotted rgba(232,230,224,0.22);padding:2rem;">'
            '<h2 style="color:#f5d90a;margin-bottom:1rem;letter-spacing:0;">KING — RESET PASSWORD</h2>'
            f'<p>Someone (IP <strong>{safe_ip}</strong>) requested an admin password reset at {when} UTC.</p>'
            f'<p style="margin:1.5rem 0;"><a href="{safe_link}" style="display:inline-block;padding:0.75rem 1.5rem;background:#f5d90a;color:#0a0a0a;text-decoration:none;font-weight:600;letter-spacing:0;">Reset the password</a></p>'
            f'<p style="color:#888;font-size:0.85rem;">The link expires in <strong>{ttl_minutes} minutes</strong> and can only be used once.</p>'
            '<p style="color:#888;font-size:0.85rem;">If this wasn\'t you, ignore this email — the current password remains valid.</p>'
            f'<p style="color:#888;font-size:0.75rem;margin-top:1.5rem;word-break:break-all;font-family:monospace;">{safe_link}</p>'
            '</div></body></html>'
        )
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, recipient, msg.as_string())
        log.info("password reset email sent to %s (ip=%s)", recipient, requester_ip)
    except Exception as e:
        log.error("password reset email failed: %s", e)
