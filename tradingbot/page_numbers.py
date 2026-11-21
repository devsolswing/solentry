"""Page numbers for the SolEntry app — the single source of truth.

Every full page gets one stable number, in navigation order. The
number is shown in the top-left corner of the page as a "Page N"
label (see templates/_page_id.html, rendered from the base template
topbar — the login page renders the same partial in its own minimal
topbar, since it suppresses the shared header). The number comes
from here on every page: no template hardcodes one.

Popups, overlays, confirm screens and API/action endpoints are not
pages and get no number — page_number_for() returns None for them,
and no label renders.
"""

PAGE_NUMBERS = {
    "login": 1,
    "signup": 2,
    "wallet": 3,
    "dashboard": 4,
    "coin_detail": 5,
    "bots": 6,
    "bot_detail": 7,
    "bot_edit": 8,
    "bot_create": 9,
    "trenches": 10,
    "logs": 11,
    "settings": 12,
    "wallet_details": 13,
    "wallet_send": 14,
    "admin_donations": 15,
    "forgot_password": 16,
    "forgot_answer": 17,
    "forgot_reset": 18,
}

# Display names for the page picker (the "Page N" chip opens it).
# Same source of truth as PAGE_NUMBERS — no template hardcodes a
# name or a number.
PAGE_NAMES = {
    "login": "Login",
    "signup": "Sign up",
    "wallet": "Home",
    "dashboard": "Coins",
    "coin_detail": "Coin detail",
    "bots": "Bots list",
    "bot_detail": "Bot detail",
    "bot_edit": "Edit bot",
    "bot_create": "builderBot chatbox",
    "trenches": "Trenches",
    "logs": "Logs",
    "settings": "Settings",
    "wallet_details": "Wallet details",
    "wallet_send": "Send",
    "admin_donations": "Admin donations",
    "forgot_password": "Forgot password",
    "forgot_answer": "Forgot answer",
    "forgot_reset": "Forgot reset",
}

# Where the picker takes the user for each page. Most pages link to
# themselves; the detail pages have no single URL (they need a coin
# or a bot), so their picker entries load the nearest list page
# instead — Coins for a coin detail, Bots for a bot detail or edit.
PAGE_TARGETS = {
    "coin_detail": "dashboard",
    "bot_detail": "bots",
    "bot_edit": "bots",
}


def page_list():
    """Every numbered page, in number order, for the picker:
    [{"number", "endpoint", "name", "target"}]. "target" is the
    endpoint the picker entry links to (see PAGE_TARGETS)."""
    out = []
    for endpoint, number in sorted(PAGE_NUMBERS.items(),
                                   key=lambda kv: kv[1]):
        out.append({"number": number, "endpoint": endpoint,
                    "name": PAGE_NAMES[endpoint],
                    "target": PAGE_TARGETS.get(endpoint, endpoint)})
    return out

# Endpoints that render another page, not their own: the coins page
# with a coin popup open is the coins page (dashboard), so it carries
# the dashboard's number.
PAGE_ALIASES = {
    "coin_popup": "dashboard",
}


def page_number_for(endpoint):
    """Page number for a Flask endpoint name, or None when the
    endpoint is not a numbered full page."""
    if not endpoint:
        return None
    endpoint = PAGE_ALIASES.get(endpoint, endpoint)
    return PAGE_NUMBERS.get(endpoint)
