"""ADR-V6-029 D3: Feishu end-to-end smoke — the 桶D production-ready seam.

This file is the **production-ready** (L2) counterpart to
``test_six_channel_acceptance.py`` (L1 code-ready). It exercises the *real* Feishu
API and therefore needs real credentials. Per ADR-V6-022 line 138, real feishu
end-to-end smoke is explicitly **桶D** — it cannot be self-completed without a
registered Feishu app + live bot, and asserting it green without those would be
fake-green.

Gating: every test skips immediately unless ``FEISHU_APP_ID`` **and**
``FEISHU_APP_SECRET`` are in the environment. A skip is an honest signal (not
pass, not fail). CI never turns these green on its own.

To run (once the founder has provisioned a Feishu app)::

    FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx \\
        .venv/bin/python -m pytest tests/gateway/test_feishu_live_smoke.py -m live_feishu -v

Full manual end-to-end runbook (beyond probe_bot — the websocket round-trip that
no automated test can fully own because it needs a human to send a DM in Feishu):

1. Set FEISHU_APP_ID / FEISHU_APP_SECRET (+ optional FEISHU_DOMAIN=lark for intl).
2. ``hermes gateway setup`` → Feishu → confirm ``probe_bot`` verifies the bot
   (this file asserts that step programmatically).
3. Enable the Feishu platform on the desktop Channels page → restart gateway.
4. In the Feishu app, DM the bot a short memo. Confirm the gateway log shows the
   inbound event hitting the Atomizer (an atom lands in PTG) and the bot replies.
5. That inbound→agent→outbound round-trip is the L2 'production-ready' bar per
   ADR-V6-029 D1. Record the outcome (timestamp + memo + reply) as the 桶D
   evidence; this file alone only proves 'credentials valid + bot reachable'.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live_feishu

_FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
_FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")
_HAS_REAL_CREDS = bool(_FEISHU_APP_ID and _FEISHU_APP_SECRET)

_SKIP_REASON = (
    "needs real FEISHU_APP_ID/FEISHU_APP_SECRET (桶D — provision a Feishu app; "
    "see module docstring runbook)"
)


@pytest.mark.skipif(not _HAS_REAL_CREDS, reason=_SKIP_REASON)
def test_probe_bot_reaches_live_feishu() -> None:
    """Minimal credentialed smoke: the app credentials are valid and the bot is
    reachable on the real Feishu API. This is the automated floor of the L2 bar;
    the full websocket round-trip is the manual runbook above."""
    from plugins.platforms.feishu.adapter import probe_bot

    domain = os.getenv("FEISHU_DOMAIN", "feishu")
    bot_info = probe_bot(_FEISHU_APP_ID, _FEISHU_APP_SECRET, domain)
    assert bot_info is not None, (
        "probe_bot returned None — credentials rejected or bot unreachable on "
        f"domain={domain}"
    )
    # probe_bot returns at least the bot identity when the creds are valid.
    assert bot_info.get("bot_name") or bot_info.get("bot_open_id"), (
        f"probe_bot reached Feishu but returned no bot identity: {bot_info!r}"
    )
