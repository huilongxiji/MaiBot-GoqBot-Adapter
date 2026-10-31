from __future__ import annotations

import json

import pytest

from conftest import ROOT, load

config = load("config")
constants = load("constants")
plugin_mod = load("plugin")


def schema():
    return plugin_mod.GoqbotAdapterPlugin().get_webui_config_schema()["sections"]


def test_defaults():
    defaults = plugin_mod.GoqbotAdapterPlugin().get_default_config()
    assert defaults["plugin"]["enabled"] is False
    assert defaults["plugin"]["platform"] == "qq"
    assert defaults["connection"] == {
        "mode": "正向",
        "address": "ws://127.0.0.1:6700",
        "access_token": "",
        "reconnect_delay_sec": 5.0,
        "heartbeat_interval_sec": 30.0,
        "action_timeout_sec": 30.0,
    }
    assert defaults["active"] == {
        "group_enabled": False,
        "group_max_per_window": 5,
        "group_window_sec": 600,
        "private_enabled": False,
    }


def test_connection_section_layout():
    fields = schema()["connection"]["fields"]
    visible = sorted((f for f in fields.values() if not f["hidden"]), key=lambda f: f["order"])
    assert [f["label"] for f in visible] == [
        "连接方式", "地址", "访问令牌", "重连等待（秒）", "心跳间隔（秒）", "动作超时（秒）",
    ]
    assert fields["mode"]["ui_type"] == "select" and fields["mode"]["choices"] == ["正向", "反向"]
    assert fields["address"]["ui_type"] == "text"
    assert fields["access_token"]["ui_type"] == "password"
    for name in ("reconnect_delay_sec", "heartbeat_interval_sec", "action_timeout_sec"):
        assert fields[name]["ui_type"] == "number" and fields[name]["min"] is not None


def test_active_section_layout():
    section = schema()["active"]
    visible = sorted(section["fields"].values(), key=lambda f: f["order"])
    assert [f["label"] for f in visible] == ["私聊主动消息", "群聊主动消息", "单群最多条数", "时间窗口（秒）"]
    for needle in ("60 条/分钟", "1000 条", "5 分钟内最多回复 5 次", "60 分钟内最多 4 次"):
        assert needle in section["description"]


def test_other_sections():
    sections = schema()
    assert [n for n, _ in sorted(sections.items(), key=lambda kv: kv[1]["order"])] == [
        "plugin", "connection", "active", "message", "tools", "mute", "api", "debug",
    ]
    plugin_fields = sections["plugin"]["fields"]
    assert plugin_fields["platform"]["label"] == "连接标识"
    widgets = plugin_mod.GoqbotAdapterPlugin().get_webui_config_schema()["runtime_widgets"]
    assert {w["id"] for w in widgets} == {"connection", "reconnect"}


def test_feature_sections_default_off():
    defaults = plugin_mod.GoqbotAdapterPlugin().get_default_config()
    assert all(v is False for k, v in defaults["tools"].items() if k.endswith("_enabled"))
    assert defaults["tools"]["wakeup_block_chat_trigger"] is True
    assert defaults["mute"] == {"trigger_users": [], "protected_users": [], "allowed_groups": [], "max_sec": 3600}
    assert all(v is False for v in defaults["api"].values())
    sections = schema()
    tools = sorted(sections["tools"]["fields"].values(), key=lambda f: f["order"])
    assert [f["label"] for f in tools] == [
        "Markdown 消息", "图文卡片", "互动召回", "禁止聊天触发召回", "主动消息权限查询", "撤回消息", "群成员禁言",
    ]
    mute = sorted(sections["mute"]["fields"].values(), key=lambda f: f["order"])
    assert [f["label"] for f in mute] == ["允许触发的用户", "禁言保护名单", "允许禁言的群", "最长禁言时长（秒）"]
    assert "留空时禁言工具不会执行" in sections["mute"]["description"]
    api = sorted(sections["api"]["fields"].values(), key=lambda f: f["order"])
    assert [f["label"] for f in api] == ["消息发送", "素材库", "菜单与指令面板", "状态查询", "群管理", "通用动作透传"]


def test_old_config_gets_feature_defaults():
    settings = config.GoqbotAdapterSettings.model_validate(
        {"plugin": {"enabled": True, "config_version": "1.1.0"}, "connection": {"address": "ws://127.0.0.1:6700"}}
    )
    assert settings.tools.markdown_enabled is False and settings.api.raw_enabled is False
    assert settings.runtime_config_error() == ""


def test_migrate_mute_max_sec_from_0_2_0():
    settings = config.GoqbotAdapterSettings.model_validate(
        {"plugin": {"config_version": "1.1.0"}, "tools": {"mute_enabled": True, "mute_max_sec": 900}}
    )
    assert settings.mute.max_sec == 900 and settings.tools.mute_enabled is True
    ids = config.MuteConfig(trigger_users=[" qq:123 ", "123", "", "456"], allowed_groups="7，8")
    assert ids.trigger_users == ["123", "456"] and ids.allowed_groups == ["7", "8"]


def test_platform_validation():
    assert config.PluginOptions(platform=" QQ_Official ").platform == "qq_official"
    assert config.PluginOptions(platform="").platform == "qq"
    with pytest.raises(ValueError):
        config.PluginOptions(platform="webui")


@pytest.mark.parametrize(
    "mode, address, expected",
    [
        ("正向", "ws://127.0.0.1:6700", ("ws", "127.0.0.1", 6700, "/")),
        ("forward", "wss://bot.example.com/ws", ("wss", "bot.example.com", 443, "/ws")),
        ("反向", "ws://0.0.0.0:8095/onebot/v11/ws", ("ws", "0.0.0.0", 8095, "/onebot/v11/ws")),
    ],
)
def test_parse_address(mode, address, expected):
    conn = config.ConnectionConfig(mode=mode, address=address)
    parsed = conn.parse_address()
    assert (parsed.scheme, parsed.host, parsed.port, parsed.path) == expected


@pytest.mark.parametrize(
    "mode, address, needle",
    [
        ("正向", "http://127.0.0.1:6700", "地址无效"),
        ("正向", "127.0.0.1:6700", "地址无效"),
        ("反向", "wss://0.0.0.0:8095/ws", "不支持 wss"),
        ("反向", "ws://0.0.0.0/ws", "需要写明端口"),
        ("反向", "ws://0.0.0.0:99999/ws", "端口无效"),
    ],
)
def test_address_errors(mode, address, needle):
    settings = config.GoqbotAdapterSettings(connection={"mode": mode, "address": address})
    assert needle in settings.runtime_config_error()


def _normalize(raw):
    """模拟 Runner：先用默认值补齐，再交给模型校验（与 SDK normalize_plugin_config 一致）。"""

    plugin = plugin_mod.GoqbotAdapterPlugin()
    normalized, changed = plugin.normalize_plugin_config(raw)
    return normalized, changed


def test_migrate_v010_reverse_config():
    normalized, changed = _normalize(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "connection": {
                "platform": "qq_official",
                "mode": "reverse",
                "forward_url": "ws://127.0.0.1:6700",
                "reverse_host": "0.0.0.0",
                "reverse_port": 18095,
                "reverse_path": "onebot",
                "access_token": "tok",
            },
        }
    )
    assert changed
    assert normalized["plugin"]["platform"] == "qq_official"
    assert normalized["plugin"]["config_version"] == constants.SUPPORTED_CONFIG_VERSION
    conn = normalized["connection"]
    assert conn["mode"] == "反向" and conn["address"] == "ws://0.0.0.0:18095/onebot"
    assert conn["access_token"] == "tok"
    assert not {"forward_url", "reverse_host", "reverse_port", "reverse_path", "platform"} & set(conn)
    settings = config.GoqbotAdapterSettings.model_validate(normalized)
    assert settings.runtime_config_error() == ""


def test_migrate_v010_forward_config():
    normalized, _ = _normalize(
        {
            "plugin": {"config_version": "1.0.0"},
            "connection": {"mode": "forward", "forward_url": "ws://10.0.0.2:6700"},
        }
    )
    assert normalized["connection"]["mode"] == "正向"
    assert normalized["connection"]["address"] == "ws://10.0.0.2:6700"
    assert normalized["plugin"]["platform"] == "qq"


def test_current_config_is_untouched():
    current = plugin_mod.GoqbotAdapterPlugin().get_default_config()
    current["connection"]["address"] = "ws://192.168.1.2:6700"
    normalized, changed = _normalize(current)
    assert not changed and normalized == current


def test_manifest_matches_plugin_id():
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == constants.PLUGIN_ID == "onebot-ws.goqbot-adapter"
    assert manifest["manifest_version"] == 2
