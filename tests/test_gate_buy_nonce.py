"""Buy $GATE popup: its script must carry the CSP nonce.

Regression guard (2026-11-24): the popup's <script> had no nonce, so
the app's Content-Security-Policy (script-src 'self' 'nonce-...' only)
blocked it on every page. The [data-gate-buy-open] binding never
attached, so tapping Buy $GATE — the wide button or the jerry-can —
did nothing. The popup was never verified opening live.
"""

import pathlib

TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "tradingbot"
    / "templates"
    / "_gate_buy.html"
)


def _src():
    return TEMPLATE.read_text(encoding="utf-8")


def test_gate_buy_script_has_csp_nonce():
    assert '<script nonce="{{ csp_nonce }}">' in _src()


def test_gate_buy_popup_still_wired():
    src = _src()
    assert "data-gate-buy-open" in src
    assert "getElementById('gate-buy-popup')" in src
    assert "gate_bundles" in src
