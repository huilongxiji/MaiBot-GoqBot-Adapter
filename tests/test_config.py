from __future__ import annotations

import json

import pytest

from conftest import ROOT, load

config = load("config")
plugin_mod = load("plugin")


def test_defaults_and_schema():
    plugin = plugin_mod.GoqbotAdapterPlugin()
    defaults = plugin.get_default_config()
    assert defaults["plugin"]["enabled"] is False
    assert defaults["connection"]["platform"] == "qq"
    assert defaults["connection"]["mode"] == "forward"
    assert defaults["active"] == {
        "group_enabled": False,
        "group_max_per_window": 5,
        "group_window_sec": 600,
        "private_enabled": False,
    }
    schema = plugin.get_webui_config_schema()
    sections = schema["sections"]
    assert set(sections) >= {"plugin", "connection", "active", "message", "debug"}
    # 主动消息分组说明里要明确写出官方限制
    desc = sections["active"]["description"]
    for needle in ("60 条/分钟", "1000 条", "5 分钟内最多回复 5 次", "60 分钟内最多 4 次"):
        assert needle in desc
    assert sections["connection"]["fields"]["mode"]["choices"] == ["forward", "reverse"]
    assert sections["connection"]["fields"]["access_token"]["input_type"] == "password"
    assert {w["id"] for w in schema["runtime_widgets"]} == {"connection", "reconnect"}


def test_platform_validation():
    assert config.ConnectionConfig(platform=" QQ_Official ").platform == "qq_official"
    assert config.ConnectionConfig(platform="").platform == "qq"
    with pytest.raises(ValueError):
        config.ConnectionConfig(platform="webui")


def test_runtime_config_error():
    s = config.GoqbotAdapterSettings()
    assert s.runtime_config_error() == ""
    s = config.GoqbotAdapterSettings(connection={"forward_url": "http://x"})
    assert "正向地址无效" in s.runtime_config_error()
    s = config.GoqbotAdapterSettings(connection={"mode": "reverse", "reverse_path": "ws"})
    assert s.connection.reverse_path == "/ws" and s.runtime_config_error() == ""


def test_manifest_matches_plugin_id():
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == load("constants").PLUGIN_ID == "onebot-ws.goqbot-adapter"
    assert manifest["manifest_version"] == 2
