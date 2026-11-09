"""Outbound verification-code senders: Twilio (SMS) and Resend (email).

Providers are configured ONLY through env vars (see Config) — no keys
or phone numbers live in code or in the repo. If a provider is not
configured, its sender returns False and the caller must say so
plainly; a code is never faked as sent.

Nothing here logs the message body, the recipient, or any credential.
Both senders use the standard library only (urllib) so the deploy
needs no new dependency.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT_SECONDS = 15


def sms_configured(cfg) -> bool:
    return bool(cfg.twilio_account_sid and cfg.twilio_auth_token
                and cfg.twilio_from_number)


def email_configured(cfg) -> bool:
    return bool(cfg.resend_api_key)


def send_sms(cfg, to: str, body: str) -> bool:
    """Send one SMS via Twilio. False when unconfigured or on any error."""
    if not sms_configured(cfg):
        return False
    url = ("https://api.twilio.com/2010-04-01/Accounts/"
           f"{cfg.twilio_account_sid}/Messages.json")
    data = urllib.parse.urlencode({
        "To": to,
        "From": cfg.twilio_from_number,
        "Body": body,
    }).encode("utf-8")
    credentials = base64.b64encode(
        f"{cfg.twilio_account_sid}:{cfg.twilio_auth_token}"
        .encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Authorization", f"Basic {credentials}")
    request.add_header("Content-Type",
                       "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def send_email(cfg, to: str, subject: str, text: str) -> bool:
    """Send one email via Resend. False when unconfigured or on any error."""
    if not email_configured(cfg):
        return False
    payload = json.dumps({
        "from": cfg.resend_from_email,
        "to": [to],
        "subject": subject,
        "text": text,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {cfg.resend_api_key}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
