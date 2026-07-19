"""ADR-V6-029 L1 acceptance: the '6 channels code-ready' Gate as a runnable CI invariant.

Converts the verbal '6 messenger channels are done' claim (ADR-V6-022 lens 10)
into enforced structure assertions that need **no credentials**. If any channel's
adapter is removed, its default-enable flips on, or it drops out of the desktop
catalog, this file goes red — preventing the '6 channels usable' fake-green that
ADR-V6-022 explicitly flagged ('口径易假绿，须 ADR 钉死验收').

The **production-ready** bar (real end-to-end smoke on >=1 channel) is a separate
桶D matter covered by ``test_feishu_live_smoke.py`` — deliberately NOT asserted
here, because it cannot pass without real credentials and asserting it would
itself be fake-green.
"""
from __future__ import annotations

import importlib.util

from gateway.config import Platform, PlatformConfig

# The canonical 6-messenger set per ADR-V6-022 lens 10 / ADR-V6-029 D1.
SIX_CHANNELS = ("telegram", "discord", "slack", "whatsapp", "feishu", "dingtalk")

# 5 of 6 connect via a manually-entered credential (required_env non-empty).
# WhatsApp is the exception: it pairs via an on-device bridge QR scan, so it has
# no required token — the bridge scan flow IS its connect path.
CREDENTIALED_CHANNELS = ("telegram", "discord", "slack", "feishu", "dingtalk")


def _catalog() -> dict[str, dict]:
    from hermes_cli.web_server import _messaging_platform_catalog

    return {entry["id"]: entry for entry in _messaging_platform_catalog()}


class TestSixChannelCodeReady:
    """L1 code-ready Gate — runnable without any credentials."""

    def test_all_six_are_builtin_platform_members(self):
        members = {m.value for m in Platform}
        missing = set(SIX_CHANNELS) - members
        assert not missing, f"canonical channel(s) dropped from Platform enum: {sorted(missing)}"

    def test_all_six_adapter_modules_exist(self):
        missing = []
        for ch in SIX_CHANNELS:
            if importlib.util.find_spec(f"plugins.platforms.{ch}.adapter") is None:
                missing.append(ch)
        assert not missing, f"adapter module(s) missing: {missing}"

    def test_platforms_disabled_by_default(self):
        # Anti-fake-green invariant: no channel auto-enables without its credential
        # env. enabled defaults False at the dataclass level; auto-enable only
        # happens inside load_gateway_config() when the cred env is actually set.
        assert PlatformConfig().enabled is False

    def test_all_six_in_desktop_catalog(self):
        catalog_ids = set(_catalog())
        missing = set(SIX_CHANNELS) - catalog_ids
        assert not missing, f"channel(s) missing from desktop catalog: {sorted(missing)}"

    def test_credentialed_channels_declare_required_token(self):
        catalog = _catalog()
        for ch in CREDENTIALED_CHANNELS:
            required = catalog[ch].get("required_env") or ()
            assert required, (
                f"{ch} declares no required_env — its connect form has no mandatory "
                "credential field"
            )

    def test_whatsapp_bridge_pairing_present(self):
        # WhatsApp pairs via bridge QR scan (no manual token). It must still appear
        # in the catalog so the connect path exists.
        assert "whatsapp" in _catalog(), "whatsapp bridge dropped from desktop catalog"


class TestFeishuConnectForm:
    """ADR-V6-029 D2: the desktop 'connect feishu' form already exists via the
    data-driven catalog. Pin its shape so a regression can't silently empty it
    (which would re-open the 'no enable UI' fake-green)."""

    def test_feishu_catalog_entry_has_credentials_and_docs(self):
        feishu = _catalog()["feishu"]
        required = set(feishu.get("required_env") or ())
        assert {"FEISHU_APP_ID", "FEISHU_APP_SECRET"} <= required
        assert feishu.get("docs_url"), "feishu card must link a setup guide"

    def test_feishu_plugin_manifest_declares_credentials(self):
        # plugin.yaml is the second source of the connect form's fields; both the
        # APP_ID and APP_SECRET must be declared so the card renders inputs.
        spec = importlib.util.find_spec("plugins.platforms.feishu")
        assert spec and spec.submodule_search_locations
        from pathlib import Path

        plugin_yaml = Path(next(iter(spec.submodule_search_locations))) / "plugin.yaml"
        text = plugin_yaml.read_text(encoding="utf-8")
        assert "FEISHU_APP_ID" in text and "FEISHU_APP_SECRET" in text
