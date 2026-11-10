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
    "pnl": 11,
    "settings": 12,
    "wallet_details": 13,
    "wallet_send": 14,
    "admin_donations": 15,
    "forgot_password": 16,
    "forgot_answer": 17,
    "forgot_reset": 18,
}

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
