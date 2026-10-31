"""聊天工具与开放接口：开关、注册信息与对 goqbot 动作的调用。"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio
import time

from maibot_sdk.context import PluginContext

from conftest import load

plugin_mod = load("plugin")
constants = load("constants")
tools_mod = load("tools")
mute_mod = load("mute")

SELF_ID = "10001"


class FakeHost:
    def __init__(self) -> None:
        self.caps: List[Dict[str, Any]] = []
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.recent: Any = []

    async def rpc(self, method: str, plugin_id: str, payload: Dict[str, Any], **_: Any) -> Any:
        assert method == "cap.call", method
        cap, args = payload["capability"], payload["args"]
        self.caps.append({"capability": cap, **args})
        if cap == "message.get_by_id":
            return {"success": True, "message": self.messages.get(args["message_id"])}
        if cap == "message.get_recent":
            if isinstance(self.recent, Exception):
                return {"success": False, "error": str(self.recent)}
            return {"success": True, "messages": self.recent}
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


def make(tools: Dict[str, Any] | None = None, api: Dict[str, Any] | None = None, platform: str = "qq",
         mute: Dict[str, Any] | None = None):
    host = FakeHost()
    plugin = plugin_mod.GoqbotAdapterPlugin()
    plugin._set_context(PluginContext(constants.PLUGIN_ID, rpc_call=host.rpc))
    cfg = plugin.get_default_config()
    cfg["plugin"]["platform"] = platform
    cfg["tools"].update(tools or {})
    cfg["api"].update(api or {})
    cfg["mute"].update(mute or {})
    plugin.set_plugin_config(cfg)
    transport = FakeTransport()
    plugin._transport = transport
    plugin._account_id = SELF_ID
    return plugin, host, transport


GROUP_CTX = {"stream_id": "s1", "group_id": "2002", "user_id": "3003", "platform": "qq"}
PRIVATE_CTX = {"stream_id": "s2", "user_id": "3003", "platform": "qq"}


def run(coro):
    return asyncio.run(coro)


def _record(user_id: str, group_id: str = "2002", ts: float | None = None) -> Dict[str, Any]:
    info: Dict[str, Any] = {"user_info": {"user_id": user_id, "user_nickname": "小明"}}
    if group_id:
        info["group_info"] = {"group_id": group_id}
    return {"timestamp": str(time.time() if ts is None else ts), "message_info": info}


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


def test_send_wakeup_only_current_user_and_not_chat_triggered():
    plugin, host, transport = make(tools={"wakeup_enabled": True})
    # 群聊里不能用，也不能指定别人
    assert run(plugin.tool_send_wakeup(content="在吗", **GROUP_CTX))["success"] is False
    tools = {c["name"]: c for c in plugin.get_components() if c["type"] == "TOOL"}
    assert tools["goqbot_send_wakeup"]["chat_scope"] == "private"
    assert [p["name"] for p in tools["goqbot_send_wakeup"]["metadata"]["parameters"]] == ["content"]
    # 用户 5 分钟前刚说过话 → 视为聊天触发，拒绝
    host.recent = [_record("3003", group_id="", ts=time.time() - 300), _record(SELF_ID, group_id="")]
    result = run(plugin.tool_send_wakeup(content="在吗", **PRIVATE_CTX))
    assert result["success"] is False and "5 分钟前" in result["error"]
    # 查询失败时宁可不发
    host.recent = RuntimeError("db down")
    assert run(plugin.tool_send_wakeup(content="在吗", **PRIVATE_CTX))["success"] is False
    assert transport.calls == []
    # 最近 60 分钟只有机器人自己的消息 → AI 自行发起，允许
    host.recent = [_record(SELF_ID, group_id="")]
    assert run(plugin.tool_send_wakeup(content="好久不见", target_user_id="4004", **PRIVATE_CTX))["success"]
    assert transport.calls[-1] == {"action": "send_private_msg", "params": {
        "user_id": 3003, "message": [{"type": "text", "data": {"text": "好久不见"}}], "is_wakeup": True}}
    recent_call = [c for c in host.caps if c["capability"] == "message.get_recent"][-1]
    assert recent_call["chat_id"] == "s2" and recent_call["hours"] == 1


def test_wakeup_chat_trigger_check_can_be_disabled():
    plugin, host, transport = make(tools={"wakeup_enabled": True, "wakeup_block_chat_trigger": False})
    host.recent = [_record("3003", group_id="", ts=time.time())]
    assert run(plugin.tool_send_wakeup(content="hi", **PRIVATE_CTX))["success"]
    assert not any(c["capability"] == "message.get_recent" for c in host.caps)


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


def test_recall_only_own_messages():
    plugin, host, transport = make(tools={"recall_enabled": True})
    host.messages["55"] = _record("3003")
    assert "自己" in run(plugin.tool_recall_message(message_id="55", **GROUP_CTX))["error"]
    host.messages["56"] = _record(SELF_ID)
    assert run(plugin.tool_recall_message(message_id="56", **GROUP_CTX))["success"]
    assert transport.calls[-1] == {"action": "delete_msg", "params": {"message_id": 56}}
    assert run(plugin.tool_recall_message(message_id="abc", **GROUP_CTX))["success"] is False


MUTE_RULES = {"trigger_users": ["8008"], "max_sec": 600}


def _cmd(*parts):
    """构造原始消息段：字符串为文本，("at", qq) 为 @。"""

    segs = []
    for part in parts:
        if isinstance(part, tuple):
            segs.append({"type": "at", "data": {"qq": part[1], "name": "小明" if part[1] == "3003" else ""}})
        else:
            segs.append({"type": "text", "data": {"text": part}})
    return segs


def _bans(transport):
    return [c for c in transport.calls if c["action"] == "set_group_ban"]


def _mute(plugin, command_id, ctx=GROUP_CTX):
    return run(plugin.tool_mute_member(command_message_id=command_id, **ctx))


def test_parse_mute_command():
    parse = mute_mod.parse_mute_command
    ok = lambda segs: parse(segs, SELF_ID)[0]  # noqa: E731
    assert ok(_cmd(("at", SELF_ID), " 禁言 ", ("at", "3003"), " 10分钟")) == mute_mod.MuteCommand("mute", "3003", "小明", 600)
    assert ok(_cmd("/禁言", ("at", "3003"))).duration == mute_mod.DEFAULT_MUTE_SEC
    assert ok(_cmd("禁言", ("at", "3003"), "2小时")).duration == 7200
    assert ok(_cmd("禁言", ("at", "3003"), "30")).duration == 1800
    assert ok(_cmd("禁言", ("at", "3003"), "45s")).duration == 45
    assert ok(_cmd("解除禁言", ("at", "3003"))).action == "unmute"
    assert ok(_cmd("解禁 ", ("at", "3003"))) == mute_mod.MuteCommand("unmute", "3003", "小明", 0)
    for segs in (
        _cmd("别禁言", ("at", "3003")),  # 不是以关键词开头
        _cmd("他刷屏了，禁言他吧", ("at", "3003")),
        _cmd("禁言"),  # 没有 @
        _cmd("禁言", ("at", "3003"), ("at", "4004")),  # @ 了两个人
        _cmd("禁言", ("at", "3003"), "一会儿"),  # 时长无法识别
        _cmd("禁言", ("at", "3003"), "0分钟"),
        _cmd("解禁", ("at", "3003"), "顺便禁言别人"),
        _cmd("禁言", ("at", "all")),
        _cmd("禁言", ("at", SELF_ID)),
        [{"type": "image", "data": {}}, *_cmd("禁言", ("at", "3003"))],
    ):
        command, reason = parse(segs, SELF_ID)
        assert command is None and reason, segs


def _feed(plugin, message_id, user_id, segs, group_id=2002):
    run(plugin._on_event({"post_type": "message", "message_type": "group", "message_id": message_id,
                          "group_id": group_id, "user_id": user_id, "message": segs}))


def make_mute(**rules):
    plugin, host, transport = make(tools={"mute_enabled": True}, mute={**MUTE_RULES, **rules})
    plugin._platform = "qq"
    plugin.ctx.gateway.route_message = _noop_route
    return plugin, host, transport


async def _noop_route(*args, **kwargs):
    return True


def test_mute_member_executes_whitelisted_command():
    plugin, _, transport = make_mute()
    _feed(plugin, 70, 8008, _cmd(("at", SELF_ID), " 禁言 ", ("at", "3003"), " 2小时"))
    result = _mute(plugin, "70")
    assert result["success"] and result["duration"] == 600 and "上限" in result["content"] and "小明" in result["content"]
    assert transport.calls[-1] == {"action": "set_group_ban",
                                   "params": {"group_id": 2002, "user_id": 3003, "duration": 600}}
    # 同一条指令不能重复执行
    assert "已经执行过" in _mute(plugin, "70")["error"]
    _feed(plugin, 71, 8008, _cmd("解禁", ("at", "3003")))
    result = _mute(plugin, "71")
    assert result["success"] and "解除" in result["content"]
    assert transport.calls[-1]["params"]["duration"] == 0


def test_mute_member_rejects_everything_else():
    plugin, _, transport = make_mute()
    _feed(plugin, 80, 3003, _cmd("禁言", ("at", "4004")))  # 普通用户发的指令
    _feed(plugin, 81, 8008, _cmd("今天谁刷屏了？", ("at", "3003")))  # 管理员的无关消息
    _feed(plugin, 82, 8008, _cmd("禁言", ("at", "3003")), group_id=9999)  # 管理员在别的群
    _feed(plugin, 83, 8008, _cmd("禁言", ("at", SELF_ID)))
    cases = {"": "缺少", "80": "允许触发", "81": "不是有效的禁言指令", "82": "不在当前群",
             "83": "不是有效的禁言指令", "999": "不是插件近"}
    for command_id, needle in cases.items():
        result = _mute(plugin, command_id)
        assert result["success"] is False and needle in result["error"], (command_id, result)
    assert not _bans(transport)


def test_mute_command_expires():
    plugin, _, transport = make_mute()
    _feed(plugin, 70, 8008, _cmd("禁言", ("at", "3003")))
    item = plugin._group_messages.get("70")
    item.received_at -= mute_mod.COMMAND_MAX_AGE_SEC + 1
    assert "不是插件近" in _mute(plugin, "70")["error"] and not _bans(transport)


def test_mute_empty_whitelist_blocks_all():
    plugin, _, transport = make_mute(trigger_users=[])
    _feed(plugin, 70, 8008, _cmd("禁言", ("at", "3003")))
    result = _mute(plugin, "70")
    assert result["success"] is False and "未配置" in result["error"] and not _bans(transport)


def test_mute_protected_users_and_allowed_groups():
    plugin, _, transport = make_mute(protected_users=["qq:3003"], allowed_groups=["5005"])
    _feed(plugin, 70, 8008, _cmd("禁言", ("at", "3003")))
    _feed(plugin, 71, 8008, _cmd("解禁", ("at", "3003")))
    assert "允许禁言的群" in _mute(plugin, "70")["error"]
    plugin.config.mute.allowed_groups = ["2002"]
    assert "保护名单" in _mute(plugin, "70")["error"]
    assert not _bans(transport)
    assert _mute(plugin, "71")["success"]  # 保护名单不影响解除禁言


def test_mute_failure_allows_retry():
    plugin, _, transport = make_mute()
    transport.responses["set_group_ban"] = {"status": "failed", "retcode": 1500, "message": "无权限"}
    _feed(plugin, 70, 8008, _cmd("禁言", ("at", "3003")))
    assert "无权限" in _mute(plugin, "70")["error"]
    del transport.responses["set_group_ban"]
    assert _mute(plugin, "70")["success"]


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
