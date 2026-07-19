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


class TestOutboundVoiceCapability:
    """ADR-V6-034 (action 19): the outbound-voice capability matrix across the 6
    core channels, made VISIBLE + tracked (anti-fake-green).

    Empirically 5 of 6 override ``BasePlatformAdapter.send_voice`` with a real
    native implementation (telegram voice bubble / discord+slack file upload /
    whatsapp bridge audio / feishu audio attachment). Only **dingtalk** does NOT
    — its session-webhook API supports text/markdown only (no voice/file), so it
    INTENTIONALLY falls through to the base ``send_voice`` fallback notice. That
    is an honest, documented platform-API limitation, NOT a missing feature or a
    bug. (The audit ADR-V6-022 lens 10 / §2.1 pessimism assumed fewer; this pins
    the true matrix.)

    ``yuanbao`` also lacks ``send_voice`` but is a legacy non-core adapter
    (``gateway/platforms/``, not in the 6-channel acceptance set) — out of scope
    here; tracked in ADR-V6-034 as a known low-priority gap. Implementing
    dingtalk/yuanbao voice now would require an unverifiable OpenAPI/SDK
    integration (no live creds = 桶 D) — writing such code would itself be
    fake-green, so this ADR makes the gap visible instead.
    """

    def _adapter_cls(self, channel: str):
        # The concrete BasePlatformAdapter subclass DEFINED in the channel's
        # adapter module (the __module__ filter excludes imported base/helpers).
        from gateway.platforms.base import BasePlatformAdapter

        modname = f"plugins.platforms.{channel}.adapter"
        mod = importlib.import_module(modname)
        candidates = [
            obj for obj in vars(mod).values()
            if isinstance(obj, type)
            and obj.__module__ == modname
            and issubclass(obj, BasePlatformAdapter)
            and obj is not BasePlatformAdapter
        ]
        assert candidates, f"no adapter class defined in {modname}"
        if len(candidates) > 1:
            # Disambiguate by name (channel token in the class name).
            matches = [c for c in candidates if channel.lower().replace("_", "") in c.__name__.lower()]
            assert matches, f"could not pick adapter among {[c.__name__ for c in candidates]} in {modname}"
            return matches[0]
        return candidates[0]

    def test_five_of_six_core_channels_have_native_send_voice(self):
        from gateway.platforms.base import BasePlatformAdapter

        voice_capable = []
        for ch in SIX_CHANNELS:
            cls = self._adapter_cls(ch)
            if cls.send_voice is not BasePlatformAdapter.send_voice:
                voice_capable.append(ch)
        # The 5 with real native voice. If a channel adds/drops voice support,
        # this assertion goes red — a deliberate, reviewed matrix change.
        assert set(voice_capable) == {"telegram", "discord", "slack", "whatsapp", "feishu"}, (
            f"outbound-voice matrix drift; voice-capable={sorted(voice_capable)}"
        )

    def test_dingtalk_lacks_send_voice_intentionally_webhook_limited(self):
        # dingtalk session-webhook = text/markdown only (no voice/file). The base
        # fallback ("⚠️ Couldn't deliver the audio attachment.") is the CORRECT
        # graceful degradation, not a gap to silently fill. Pinned so a future
        # naive `send_voice` override that doesn't actually work can't sneak in.
        from gateway.platforms.base import BasePlatformAdapter

        cls = self._adapter_cls("dingtalk")
        assert cls.send_voice is BasePlatformAdapter.send_voice, (
            "dingtalk send_voice should inherit the base fallback — its session-webhook "
            "API supports text/markdown only; a naive override without the OpenAPI media "
            "SDK would be unverifiable fake-green (ADR-V6-034)"
        )

    def test_base_send_voice_fallback_is_honest(self):
        # The inherited fallback must NEVER leak the host-local audio_path into
        # chat (filesystem-layout leak) and must send a friendly notice (C7: no
        # silent failure — the user is told the audio wasn't delivered).
        import inspect

        from gateway.platforms.base import BasePlatformAdapter

        src = inspect.getsource(BasePlatformAdapter.send_voice)
        assert "Couldn't deliver the audio" in src, "base fallback must send a friendly notice"
        assert "audio_path is intentionally NOT included" in src or "NOT included" in src, (
            "base fallback must document it does NOT echo the host-local audio_path"
        )
