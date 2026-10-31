"""聊天工具与开放接口：开关、注册信息与对 goqbot 动作的调用。"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio

from maibot_sdk.context import PluginContext

from conftest import load

plugin_mod = load("plugin")
constants = load("constants")
tools_mod = load("tools")

SELF_ID = "10001"


class FakeHost:
    def __init__(self) -> None:
        self.caps: List[Dict[str, Any]] = []
        self.messages: Dict[str, Dict[str, Any]] = {}

    async def rpc(self, method: str, plugin_id: str, payload: Dict[str, Any], **_: Any) -> Any:
        assert method == "cap.call", method
        cap, args = payload["capability"], payload["args"]
        self.caps.append({"capability": cap, **args})
        if cap == "message.get_by_id":
            return {"success": True, "message": self.messages.get(args["message_id"])}
        return {"success": True}


class FakeTransport:
    def __init__(self, responses: Dict[str, Dict[str, Any]] | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.responses = responses or {}

    async def call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"action": action, "params": params})
        if action in self.responses:
            return self.responses[action]
        return {"status": "ok", "retcode": 0, "data": {"message_id": 900}}

    async def stop(self) -> None:
        return None


def make(tools: Dict[str, Any] | None = None, api: Dict[str, Any] | None = None, platform: str = "qq"):
    host = FakeHost()
    plugin = plugin_mod.GoqbotAdapterPlugin()
    plugin._set_context(PluginContext(constants.PLUGIN_ID, rpc_call=host.rpc))
    cfg = plugin.get_default_config()
    cfg["plugin"]["platform"] = platform
    cfg["tools"].update(tools or {})
    cfg["api"].update(api or {})
    plugin.set_plugin_config(cfg)
    transport = FakeTransport()
    plugin._transport = transport
    plugin._account_id = SELF_ID
    return plugin, host, transport


GROUP_CTX = {"stream_id": "s1", "group_id": "2002", "user_id": "3003", "platform": "qq"}
PRIVATE_CTX = {"stream_id": "s2", "user_id": "3003", "platform": "qq"}


def run(coro):
    return asyncio.run(coro)


# ── 注册与开关 ──────────────────────────────────────────────────


def test_components_follow_switches():
    plugin, _, _ = make(tools={"markdown_enabled": True, "mute_enabled": True}, api={"media_enabled": True},
                        platform="qq_official")
    components = plugin.get_components()
    tools = {c["name"]: c for c in components if c["type"] == "TOOL"}
    assert set(tools) == set(tools_mod.TOOL_SWITCHES)
    assert tools["goqbot_send_markdown"]["metadata"]["enabled"] is True
    assert tools["goqbot_send_card"]["metadata"]["enabled"] is False
    assert all(t["metadata"]["platforms"] == ["qq_official"] for t in tools.values())
    assert tools["goqbot_mute_member"]["chat_scope"] == "group"
    assert tools["goqbot_send_markdown"]["chat_scope"] == "all"
    apis = sorted(c["name"] for c in components if c["type"] == "API")
    assert apis == [
        "adapter.goqbot.media.delete_image", "adapter.goqbot.media.get_image",
        "adapter.goqbot.media.list_images", "adapter.goqbot.media.upload_image",
    ]
    assert all(c["metadata"]["public"] for c in components if c["type"] == "API")


def test_sync_features_toggles_tools_and_replaces_apis():
    plugin, host, _ = make(tools={"recall_enabled": True}, api={"raw_enabled": True})
    run(plugin._sync_features())
    toggles = {c["name"]: c["capability"] for c in host.caps if c["capability"].startswith("component.")}
    assert toggles[f"{constants.PLUGIN_ID}.goqbot_recall_message"] == "component.enable"
    assert toggles[f"{constants.PLUGIN_ID}.goqbot_send_markdown"] == "component.disable"
    assert len(toggles) == len(tools_mod.TOOL_SWITCHES)
    replaced = [c for c in host.caps if c["capability"] == "api.replace_dynamic"][-1]
    assert [a["name"] for a in replaced["apis"]] == ["adapter.goqbot.raw.call_action"]


# ── 工具 ────────────────────────────────────────────────────────


def test_tool_guards():
    plugin, _, transport = make()
    result = run(plugin.tool_send_markdown(content="hi", **GROUP_CTX))
    assert result["success"] is False and "关闭" in result["error"]
    plugin, _, transport = make(tools={"markdown_enabled": True})
    result = run(plugin.tool_send_markdown(content="hi", **{**GROUP_CTX, "platform": "napcat"}))
    assert result["success"] is False and "不属于" in result["error"]
    plugin._account_id = None
    result = run(plugin.tool_send_markdown(content="hi", **GROUP_CTX))
    assert result["success"] is False and "未连接" in result["error"]
    assert transport.calls == []


def test_send_markdown_with_buttons():
    plugin, _, transport = make(tools={"markdown_enabled": True})
    buttons = [{"label": "官网", "type": "link", "data": "https://example.com"},
               {"label": "帮助", "type": "command", "data": "/help"},
               {"label": "填写", "type": "command", "data": "/查 ", "enter": False},
               {"label": "第四个", "type": "command", "data": "4"}]
    result = run(plugin.tool_send_markdown(content="# 标题", buttons=buttons, **GROUP_CTX))
    assert result["success"] and result["mode"] == "passive"
    call = transport.calls[-1]
    assert call["action"] == "send_group_msg" and call["params"]["group_id"] == 2002
    assert call["params"]["passive_only"] is True
    seg = call["params"]["message"][0]
    assert seg["type"] == "markdown" and seg["data"]["data"]["markdown"] == {"content": "# 标题"}
    rows = seg["data"]["data"]["keyboard"]["content"]["rows"]
    assert [len(r["buttons"]) for r in rows] == [3, 1]
    first, _, third = rows[0]["buttons"]
    assert first["action"]["type"] == 0 and "enter" not in first["action"]
    assert third["action"] == {"type": 2, "permission": {"type": 2}, "data": "/查",
                               "unsupport_tips": "当前 QQ 版本不支持该按钮", "enter": False}


def test_markdown_button_validation():
    assert tools_mod.build_keyboard([{"label": "x", "type": "link", "data": "ftp://a"}])[1]
    assert tools_mod.build_keyboard([{"label": "x", "type": "callback", "data": "a"}])[1]
    assert tools_mod.build_keyboard([{"label": "x", "data": "a"}] * 16)[1]
    assert tools_mod.build_keyboard(None) == (None, "")


def test_markdown_respects_active_switch():
    plugin, _, transport = make(tools={"markdown_enabled": True})
    transport.responses["send_private_msg"] = {"status": "failed", "retcode": 1460, "data": None}
    result = run(plugin.tool_send_markdown(content="hi", **PRIVATE_CTX))
    assert result["success"] is False and result["mode"] == "dropped"
    assert [c["action"] for c in transport.calls] == ["send_private_msg"]


def test_send_card():
    plugin, _, transport = make(tools={"card_enabled": True})
    bad = run(plugin.tool_send_card(title="t", url="http://a", image_url="https://b", **GROUP_CTX))
    assert bad["success"] is False and "https" in bad["error"]
    ok = run(plugin.tool_send_card(title="标题", url="https://a", image_url="https://b/c.png", **GROUP_CTX))
    assert ok["success"]
    seg = transport.calls[-1]["params"]["message"][0]
    assert seg == {"type": "share", "data": {"title": "标题", "description": "标题",
                                             "url": "https://a", "pic_url": "https://b/c.png"}}


def test_send_wakeup():
    plugin, _, transport = make(tools={"wakeup_enabled": True})
    assert run(plugin.tool_send_wakeup(content="在吗", **GROUP_CTX))["success"] is False
    result = run(plugin.tool_send_wakeup(content="在吗", **PRIVATE_CTX))
    assert result["success"]
    assert transport.calls[-1] == {"action": "send_private_msg", "params": {
        "user_id": 3003, "message": [{"type": "text", "data": {"text": "在吗"}}], "is_wakeup": True}}
    run(plugin.tool_send_wakeup(content="hi", target_user_id="4004", **GROUP_CTX))
    assert transport.calls[-1]["params"]["user_id"] == 4004


def test_push_state():
    plugin, _, transport = make(tools={"push_state_enabled": True})
    transport.responses["get_group_bot_state"] = {
        "status": "ok", "retcode": 0, "data": {"allow_proactive_msg": True, "member_role": "admin"}}
    result = run(plugin.tool_get_push_state(**GROUP_CTX))
    assert result["success"] and result["allow_proactive_msg"] is True and "已开启" in result["content"]
    assert "群聊主动消息：关闭" in result["content"]
    transport.responses["get_c2c_msg_state"] = {"status": "ok", "retcode": 0, "data": {"known": False}}
    result = run(plugin.tool_get_push_state(**PRIVATE_CTX))
    assert result["success"] and result["allow_proactive_msg"] is None and "未知" in result["content"]


def _record(user_id: str, group_id: str = "2002") -> Dict[str, Any]:
    return {"message_info": {"user_info": {"user_id": user_id, "user_nickname": "小明"},
                             "group_info": {"group_id": group_id}}}


def test_recall_only_own_messages():
    plugin, host, transport = make(tools={"recall_enabled": True})
    host.messages["55"] = _record("3003")
    assert "自己" in run(plugin.tool_recall_message(message_id="55", **GROUP_CTX))["error"]
    host.messages["56"] = _record(SELF_ID)
    assert run(plugin.tool_recall_message(message_id="56", **GROUP_CTX))["success"]
    assert transport.calls[-1] == {"action": "delete_msg", "params": {"message_id": 56}}
    assert run(plugin.tool_recall_message(message_id="abc", **GROUP_CTX))["success"] is False


def test_mute_member():
    plugin, host, transport = make(tools={"mute_enabled": True, "mute_max_sec": 600})
    host.messages["60"] = _record("3003")
    result = run(plugin.tool_mute_member(message_id="60", duration=99999, **GROUP_CTX))
    assert result["success"] and result["duration"] == 600 and "小明" in result["content"]
    assert transport.calls[-1] == {"action": "set_group_ban",
                                   "params": {"group_id": 2002, "user_id": 3003, "duration": 600}}
    result = run(plugin.tool_mute_member(message_id="60", duration=0, **GROUP_CTX))
    assert "解除" in result["content"]
    host.messages["61"] = _record(SELF_ID)
    assert run(plugin.tool_mute_member(message_id="61", duration=60, **GROUP_CTX))["success"] is False
    host.messages["62"] = _record("3003", group_id="9999")
    assert "不在当前群" in run(plugin.tool_mute_member(message_id="62", duration=60, **GROUP_CTX))["error"]
    assert run(plugin.tool_mute_member(message_id="404", duration=60, **GROUP_CTX))["success"] is False


# ── 开放接口 ────────────────────────────────────────────────────


def _invoke(plugin, api_name: str, **args):
    plugin.get_components()
    component = next(c for c in plugin.get_dynamic_api_components() if c["name"] == api_name)
    return run(plugin.invoke_component(component["metadata"]["handler_name"], **args))


def test_api_passthrough():
    plugin, _, transport = make(api={"media_enabled": True})
    result = _invoke(plugin, "adapter.goqbot.media.upload_image", file="https://x/y.png", name="logo",
                     force=None, __maibot_api_name__="adapter.goqbot.media.upload_image")
    assert result == {"success": True, "retcode": 0, "data": {"message_id": 900}, "message": ""}
    assert transport.calls[-1] == {"action": "upload_media_image", "params": {"file": "https://x/y.png", "name": "logo"}}


def test_api_message_send_modes():
    plugin, _, transport = make(api={"message_enabled": True})
    seg = [{"type": "markdown", "data": {"data": {"markdown": {"content": "x"}}}}]
    assert _invoke(plugin, "adapter.goqbot.message.send", message_type="group", target_id="2002", message=seg)["success"]
    assert transport.calls[-1]["params"]["passive_only"] is True
    _invoke(plugin, "adapter.goqbot.message.send", message_type="private", target_id=3003, message="hi", mode="wakeup")
    assert transport.calls[-1] == {"action": "send_private_msg", "params": {
        "user_id": 3003, "message": [{"type": "text", "data": {"text": "hi"}}], "is_wakeup": True}}
    _invoke(plugin, "adapter.goqbot.message.send", message_type="group", target_id=2002, message="hi", mode="active")
    assert transport.calls[-1]["action"] == "send_group_active_msg"
    bad = _invoke(plugin, "adapter.goqbot.message.send", message_type="group", target_id=2002, message="hi",
                  mode="wakeup")
    assert bad["success"] is False


def test_api_raw_and_disabled():
    plugin, _, transport = make(api={"raw_enabled": True})
    result = _invoke(plugin, "adapter.goqbot.raw.call_action", action="get_menu", params={})
    assert result["success"] and transport.calls[-1] == {"action": "get_menu", "params": {}}
    handler = next(c for c in plugin.get_dynamic_api_components())["metadata"]["handler_name"]
    plugin.config.api.raw_enabled = False
    result = run(plugin.invoke_component(handler, action="get_menu", params={}))
    assert result["success"] is False and "关闭" in result["message"]
