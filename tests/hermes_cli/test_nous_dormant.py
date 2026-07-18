"""V6 ADR-V6-006 regression: Nous dormant single-point kill switch.

V6 does not use the Nous provider (智谱 GLM-5.2 / DeepSeek per ADR-093).
``NOUS_DORMANT`` short-circuits ``get_nous_portal_account_info`` so every
caller (25+) sees ``logged_in=False`` and fails closed, without deleting the
77 gated references in conversation_loop. This test locks that behavior so the
kill switch cannot silently regress (铁律 C4).
"""

from __future__ import annotations

from hermes_cli.nous_account import NOUS_DORMANT, get_nous_portal_account_info


def test_nous_dormant_flag_is_enabled() -> None:
    """ADR-V6-006: the kill switch must default to True in V6."""
    assert NOUS_DORMANT is True


def test_nous_portal_account_info_fails_closed_when_dormant() -> None:
    """ADR-V6-006: dormant → every caller sees logged_in=False (fail-closed)."""
    info = get_nous_portal_account_info()
    assert info.logged_in is False
    assert info.source == "none"
