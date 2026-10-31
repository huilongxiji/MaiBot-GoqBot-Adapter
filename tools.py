"""聊天工具：把 goqbot 特有的能力以 Tool 组件提供给 MaiBot 的模型。

每个工具在配置页「聊天工具」中单独开关（默认关闭）：
- 注册时按开关写入 ``enabled``，并把 ``platforms`` 限定为本连接的连接标识；
- 加载与配置更新时调用 ``component.enable / disable`` 同步开关；
- 执行时再检查一次开关、平台与连接状态，防止被手动启用或跨平台误用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import asyncio

from maibot_sdk import Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .codec import OutboundPlan
from .transport import TransportNotConnected

# 工具名 → 配置项（ToolsConfig 上的属性名）
TOOL_SWITCHES: Dict[str, str] = {
    "goqbot_send_markdown": "markdown_enabled",
    "goqbot_send_card": "card_enabled",
    "goqbot_send_wakeup": "wakeup_enabled",
    "goqbot_get_push_state": "push_state_enabled",
    "goqbot_recall_message": "recall_enabled",
    "goqbot_mute_member": "mute_enabled",
}

MAX_BUTTONS = 15
BUTTONS_PER_ROW = 3
_BUTTON_TYPES = {"link": 0, "command": 2}


def _fail(error: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": error, **extra}


def _done(content: str, **extra: Any) -> Dict[str, Any]:
    return {"success": True, "content": content, **extra}


def _to_int(value: Any) -> Optional[int]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _response_ok(response: Mapping[str, Any]) -> bool:
    return str(response.get("status") or "").lower() == "ok" and response.get("retcode", 0) == 0


def _response_error(response: Mapping[str, Any]) -> str:
    text = str(response.get("wording") or response.get("message") or response.get("msg") or "goqbot 返回失败")
    code = response.get("retcode")
    return f"{text}（retcode={code}）" if code is not None else text


def build_keyboard(buttons: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """把模型给出的按钮列表转成 QQ keyboard 结构；返回 (keyboard, 错误信息)。"""

    if buttons in (None, "", []):
        return None, ""
    if not isinstance(buttons, list):
        return None, "buttons 必须是数组"
    if len(buttons) > MAX_BUTTONS:
        return None, f"按钮最多 {MAX_BUTTONS} 个"
    rendered: List[Dict[str, Any]] = []
    for index, item in enumerate(buttons, start=1):
        if not isinstance(item, Mapping):
            return None, f"第 {index} 个按钮格式错误"
        label = str(item.get("label") or "").strip()
        kind = str(item.get("type") or "command").strip().lower()
        data = str(item.get("data") or "").strip()
        if not label or not data:
            return None, f"第 {index} 个按钮缺少 label 或 data"
        if kind not in _BUTTON_TYPES:
            return None, f"第 {index} 个按钮 type 只能是 link 或 command"
        if kind == "link" and not data.startswith(("https://", "http://")):
            return None, f"第 {index} 个按钮是跳转按钮，data 必须是 http(s) 链接"
        action: Dict[str, Any] = {
            "type": _BUTTON_TYPES[kind],
            "permission": {"type": 2},
            "data": data,
            "unsupport_tips": "当前 QQ 版本不支持该按钮",
        }
        if kind == "command":
            action["enter"] = bool(item.get("enter", True))
        rendered.append(
            {
                "id": str(index),
                "render_data": {"label": label, "visited_label": label, "style": 1},
                "action": action,
            }
        )
    rows = [
        {"buttons": rendered[start:start + BUTTONS_PER_ROW]}
        for start in range(0, len(rendered), BUTTONS_PER_ROW)
    ]
    return {"content": {"rows": rows}}, ""


def build_card(title: Any, description: Any, url: Any, image_url: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    title_text = str(title or "").strip()
    desc_text = str(description or "").strip() or title_text
    url_text = str(url or "").strip()
    image_text = str(image_url or "").strip()
    if not 1 <= len(title_text) <= 50:
        return None, "标题长度须为 1–50 字"
    if len(desc_text) > 200:
        return None, "简介最多 200 字"
    for name, value in (("url", url_text), ("image_url", image_text)):
        if not value.startswith("https://"):
            return None, f"{name} 必须是 https 链接"
    return {
        "type": "share",
        "data": {"title": title_text, "description": desc_text, "url": url_text, "pic_url": image_text},
    }, ""


class GoqbotToolsMixin:
    """聊天工具实现；宿主类需提供 ctx、_settings()、_call_action、_sender、_account_id。"""

    # ── 公共 ────────────────────────────────────────────────────

    def _tool_guard(self, tool_name: str, context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        settings = self._settings()  # type: ignore[attr-defined]
        if not getattr(settings.tools, TOOL_SWITCHES[tool_name]):
            return _fail("该工具已在 goqbot 适配器配置中关闭")
        platform = str(context.get("platform") or "").strip().lower()
        if platform and platform != settings.plugin.platform:
            return _fail(f"当前会话不属于 goqbot 连接（{settings.plugin.platform}），无法使用该工具")
        if not self._account_id:  # type: ignore[attr-defined]
            return _fail("goqbot 未连接")
        return None

    @staticmethod
    def _chat_target(context: Mapping[str, Any]) -> Tuple[str, Optional[int]]:
        group_id = _to_int(context.get("group_id"))
        if group_id:
            return "group", group_id
        return "private", _to_int(context.get("user_id"))

    async def _tool_call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return await self._call_action(action, params)  # type: ignore[attr-defined]
        except (TransportNotConnected, asyncio.TimeoutError) as exc:
            return {"status": "failed", "retcode": -1, "message": f"goqbot 未响应: {exc or '超时'}"}

    async def _tool_send(self, context: Mapping[str, Any], segment: Dict[str, Any], what: str) -> Dict[str, Any]:
        chat_type, target_id = self._chat_target(context)
        if not target_id:
            return _fail("无法确定当前聊天的群号或用户 ID")
        plan = OutboundPlan(chat_type=chat_type, target_id=target_id, segments=[segment], dropped_types=[])
        try:
            result = await self._sender.send(plan, self._settings().active)  # type: ignore[attr-defined]
        except (TransportNotConnected, asyncio.TimeoutError) as exc:
            return _fail(f"goqbot 未响应: {exc or '超时'}")
        if not result.success:
            return _fail(f"{what}发送失败：{result.error}", retcode=result.retcode, mode=result.mode)
        return _done(f"{what}已发送", message_id=result.message_id, mode=result.mode)

    async def _message_sender(self, message_id: str, stream_id: str) -> Optional[Dict[str, str]]:
        """通过 MaiBot 聊天记录查出某条消息的发送者与所在群。"""

        try:
            record = await self.ctx.message.get_by_id(message_id, stream_id=stream_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None
        if isinstance(record, Mapping) and "message" in record and "message_info" not in record:
            record = record.get("message")
        if not isinstance(record, Mapping):
            return None
        info = record.get("message_info") if isinstance(record.get("message_info"), Mapping) else {}
        user = info.get("user_info") if isinstance(info.get("user_info"), Mapping) else {}
        group = info.get("group_info") if isinstance(info.get("group_info"), Mapping) else {}
        return {
            "user_id": str(user.get("user_id") or "").strip(),
            "name": str(user.get("user_cardname") or user.get("user_nickname") or user.get("user_id") or "").strip(),
            "group_id": str(group.get("group_id") or "").strip(),
        }

    # ── 消息类 ──────────────────────────────────────────────────

    @Tool(
        "goqbot_send_markdown",
        description=(
            "在当前 QQ 聊天中发送一条原生 Markdown 消息，可附带按钮。适合需要排版（标题、列表、加粗、代码块）"
            "或希望用户点按钮继续操作的回复；普通聊天回复不需要使用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content", param_type=ToolParamType.STRING,
                description="Markdown 正文。", required=True,
            ),
            ToolParameterInfo(
                name="buttons", param_type=ToolParamType.ARRAY, required=False,
                description=(
                    f"可选按钮，最多 {MAX_BUTTONS} 个，每行 {BUTTONS_PER_ROW} 个。type=link 时 data 为跳转链接；"
                    "type=command 时 data 为点按后代用户发出的文字（enter=false 时只填入输入框）。"
                ),
                items_schema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "按钮文字"},
                        "type": {"type": "string", "enum": ["link", "command"]},
                        "data": {"type": "string", "description": "链接或指令文字"},
                        "enter": {"type": "boolean", "description": "command 按钮是否直接发送，默认 true"},
                    },
                    "required": ["label", "type", "data"],
                },
            ),
        ],
        enabled=False,
    )
    async def tool_send_markdown(self, content: str = "", buttons: Any = None, **context: Any) -> Dict[str, Any]:
        if denied := self._tool_guard("goqbot_send_markdown", context):
            return denied
        text = str(content or "").strip()
        if not text:
            return _fail("content 不能为空")
        keyboard, error = build_keyboard(buttons)
        if error:
            return _fail(error)
        payload: Dict[str, Any] = {"markdown": {"content": text}}
        if keyboard:
            payload["keyboard"] = keyboard
        return await self._tool_send(context, {"type": "markdown", "data": {"data": payload}}, "Markdown 消息")

    @Tool(
        "goqbot_send_card",
        description="在当前 QQ 聊天中发送一张图文链接卡片（标题 + 简介 + 封面图，点按打开链接），用于分享网页。",
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING, description="标题，1–50 字。"),
            ToolParameterInfo(
                name="description", param_type=ToolParamType.STRING, required=False,
                description="简介，最多 200 字；不填则使用标题。",
            ),
            ToolParameterInfo(name="url", param_type=ToolParamType.STRING, description="点按后打开的 https 链接。"),
            ToolParameterInfo(name="image_url", param_type=ToolParamType.STRING, description="封面图的 https 链接。"),
        ],
        enabled=False,
    )
    async def tool_send_card(
        self, title: str = "", description: str = "", url: str = "", image_url: str = "", **context: Any
    ) -> Dict[str, Any]:
        if denied := self._tool_guard("goqbot_send_card", context):
            return denied
        segment, error = build_card(title, description, url, image_url)
        if segment is None:
            return _fail(error)
        return await self._tool_send(context, segment, "图文卡片")

    @Tool(
        "goqbot_send_wakeup",
        description=(
            "给一位 QQ 私聊用户发送互动召回消息，用于提醒很久没来聊天的用户。只能发给 30 天内私聊过机器人的用户，"
            "30 天内分 4 个周期（当天 / 1–3 天 / 3–7 天 / 7–30 天）每周期只能发 1 条，请谨慎使用。"
        ),
        parameters=[
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, description="要发送的文字。"),
            ToolParameterInfo(
                name="target_user_id", param_type=ToolParamType.STRING, required=False,
                description="私聊用户的 user_id；在私聊中调用时可不填，默认发给当前用户。",
            ),
        ],
        enabled=False,
    )
    async def tool_send_wakeup(self, content: str = "", target_user_id: Any = "", **context: Any) -> Dict[str, Any]:
        if denied := self._tool_guard("goqbot_send_wakeup", context):
            return denied
        text = str(content or "").strip()
        if not text:
            return _fail("content 不能为空")
        user_id = _to_int(target_user_id)
        if user_id is None:
            chat_type, current = self._chat_target(context)
            if chat_type != "private" or current is None:
                return _fail("请提供 target_user_id（私聊用户的 user_id）")
            user_id = current
        response = await self._tool_call(
            "send_private_msg",
            {"user_id": user_id, "message": [{"type": "text", "data": {"text": text}}], "is_wakeup": True},
        )
        if not _response_ok(response):
            return _fail(f"互动召回发送失败：{_response_error(response)}")
        data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
        return _done(f"已向用户 {user_id} 发送互动召回消息", message_id=str(data.get("message_id") or ""))

    # ── 查询 / 管理类 ───────────────────────────────────────────

    @Tool(
        "goqbot_get_push_state",
        description=(
            "查询当前 QQ 聊天是否允许机器人主动发消息（不针对用户刚发的消息、隔一段时间再说话时需要主动消息），"
            "以及适配器的主动消息开关。"
        ),
        parameters=[],
        enabled=False,
    )
    async def tool_get_push_state(self, **context: Any) -> Dict[str, Any]:
        if denied := self._tool_guard("goqbot_get_push_state", context):
            return denied
        active = self._settings().active  # type: ignore[attr-defined]
        chat_type, target_id = self._chat_target(context)
        if not target_id:
            return _fail("无法确定当前聊天的群号或用户 ID")
        if chat_type == "group":
            response = await self._tool_call("get_group_bot_state", {"group_id": target_id})
            if not _response_ok(response):
                return _fail(f"查询失败：{_response_error(response)}")
            data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
            allow = data.get("allow_proactive_msg")
            switch = (
                f"开启（每群 {active.group_max_per_window} 条/{active.group_window_sec} 秒）"
                if active.group_enabled else "关闭"
            )
            content = (
                f"群主{'已' if allow else '未'}开启「机器人主动在群聊内发言」；"
                f"机器人在群内的身份：{data.get('member_role') or 'member'}；适配器群聊主动消息：{switch}。"
            )
            return _done(content, allow_proactive_msg=bool(allow), plugin_switch=active.group_enabled,
                         member_role=str(data.get("member_role") or ""))
        response = await self._tool_call("get_c2c_msg_state", {"user_id": target_id})
        if not _response_ok(response):
            return _fail(f"查询失败：{_response_error(response)}")
        data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
        allow = data.get("allow_proactive_msg") if data.get("known") else None
        state = "未知（尚未收到该用户的推送设置）" if allow is None else ("允许" if allow else "已关闭")
        content = f"该用户的机器人主动推送：{state}；适配器私聊主动消息：{'开启' if active.private_enabled else '关闭'}。"
        return _done(content, allow_proactive_msg=allow, plugin_switch=active.private_enabled)

    @Tool(
        "goqbot_recall_message",
        description=(
            "撤回机器人自己在当前 QQ 聊天中发过的一条消息。QQ 官方只允许撤回机器人自己的消息，且只能撤回刚发不久的消息。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id", param_type=ToolParamType.STRING,
                description="要撤回的消息 ID（聊天记录中的消息 ID）。",
            ),
        ],
        enabled=False,
    )
    async def tool_recall_message(self, message_id: Any = "", **context: Any) -> Dict[str, Any]:
        if denied := self._tool_guard("goqbot_recall_message", context):
            return denied
        numeric_id = _to_int(message_id)
        if numeric_id is None:
            return _fail("message_id 无效")
        stream_id = str(context.get("stream_id") or context.get("chat_id") or "")
        sender = await self._message_sender(str(numeric_id), stream_id)
        if sender and sender["user_id"] and sender["user_id"] != self._account_id:  # type: ignore[attr-defined]
            return _fail("只能撤回机器人自己发送的消息")
        response = await self._tool_call("delete_msg", {"message_id": numeric_id})
        if not _response_ok(response):
            return _fail(f"撤回失败：{_response_error(response)}")
        return _done(f"消息 {numeric_id} 已撤回")

    @Tool(
        "goqbot_mute_member",
        description=(
            "在当前 QQ 群中按消息 ID 禁言该消息的发送者，duration=0 表示解除禁言。"
            "仅在刷屏、违规或用户主动要求时使用；需要机器人是群管理员。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id", param_type=ToolParamType.STRING,
                description="目标用户发送的消息 ID（聊天记录中的消息 ID）。",
            ),
            ToolParameterInfo(
                name="duration", param_type=ToolParamType.INTEGER,
                description="禁言时长（秒），0 表示解除禁言；超过配置上限时按上限处理。",
            ),
        ],
        chat_scope="group",
        enabled=False,
    )
    async def tool_mute_member(self, message_id: Any = "", duration: Any = 0, **context: Any) -> Dict[str, Any]:
        if denied := self._tool_guard("goqbot_mute_member", context):
            return denied
        chat_type, group_id = self._chat_target(context)
        if chat_type != "group" or not group_id:
            return _fail("禁言只能在群聊中使用")
        try:
            seconds = max(0, int(duration or 0))
        except (TypeError, ValueError):
            return _fail("duration 必须是整数秒")
        seconds = min(seconds, self._settings().tools.mute_max_sec)  # type: ignore[attr-defined]
        msg_id = str(message_id or "").strip()
        if not msg_id:
            return _fail("缺少 message_id")
        stream_id = str(context.get("stream_id") or context.get("chat_id") or "")
        sender = await self._message_sender(msg_id, stream_id)
        if not sender or not sender["user_id"]:
            return _fail(f"在当前聊天记录中找不到消息 {msg_id}")
        if sender["group_id"] and sender["group_id"] != str(group_id):
            return _fail("该消息不在当前群")
        if sender["user_id"] == self._account_id:  # type: ignore[attr-defined]
            return _fail("不能禁言机器人自己")
        user_id = _to_int(sender["user_id"])
        if user_id is None:
            return _fail("消息发送者 ID 无效")
        response = await self._tool_call("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": seconds})
        if not _response_ok(response):
            return _fail(f"禁言失败：{_response_error(response)}")
        who = sender["name"] or str(user_id)
        if seconds == 0:
            return _done(f"已解除 {who} 的禁言", user_id=str(user_id))
        return _done(f"已禁言 {who} {seconds} 秒", user_id=str(user_id), duration=seconds)
