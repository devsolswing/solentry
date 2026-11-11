"""Outbound email — used ONLY by the Security bot.

The Security bot is the app's only mail sender, and it only ever
sends two messages:

  * the 4-digit signup confirmation code (Create Account), and
  * the 4-digit account-recovery code (Recover Account).

Those are the ONLY two ways account information ever reaches a user
by email. There is no notification mail, no marketing mail, no
password-reset-link mail, and no other sender anywhere in the app.
If email is not configured, sending fails closed and the caller
says so plainly instead of pretending a code was sent.

Configuration (all env vars, never hardcoded, never in the repo):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM, SMTP_USE_TLS (default 1). The owner sets these on
  Railway (a Gmail app password for mintendont94@gmail.com works:
  SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USER=<the gmail
  address>, SMTP_PASSWORD=<the 16-character app password>,
  SMTP_FROM=<the gmail address>).
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage


def smtp_configured(cfg) -> bool:
    """True only when the minimum to send exists: a host and a
    From address. A missing password is caught at send time."""
    return bool(getattr(cfg, "smtp_host", "") and getattr(cfg, "smtp_from", ""))


def send_email(cfg, to: str, subject: str, body: str) -> tuple[bool, str]:
    """Send one plain-text email. Returns (True, "") on success, or
    (False, reason) on any failure — the reason is a plain message
    safe to show the user (never a credential, never a traceback).

    Fail-closed: when SMTP is not configured, or the send fails for
    any reason, nothing is sent and the caller must say so plainly.
    """
    if not smtp_configured(cfg):
        return False, "email is not set up yet"
    if not to or "@" not in to:
        return False, "bad email address"
    msg = EmailMessage()
    msg["From"] = cfg.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        port = int(getattr(cfg, "smtp_port", 587) or 587)
        use_tls = bool(getattr(cfg, "smtp_use_tls", True))
        if port == 465:
            # Implicit TLS (SMTPS).
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=15,
                                  context=context) as server:
                if getattr(cfg, "smtp_user", ""):
                    server.login(cfg.smtp_user, cfg.smtp_password or "")
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, port, timeout=15) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if getattr(cfg, "smtp_user", ""):
                    server.login(cfg.smtp_user, cfg.smtp_password or "")
                server.send_message(msg)
    except Exception as exc:
        # Never leak the exception detail (it can carry the host /
        # credentials context) — a plain refusal is enough.
        _ = exc
        return False, "could not send the email right now"
    return True, ""
