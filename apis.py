"""开放接口：把 goqbot 特有的动作以动态 API 的形式提供给其他 MaiBot 插件。

- API 名统一为 ``adapter.goqbot.<分组>.<名称>``，按分组在配置页「开放接口」中单独开关（默认关闭）；
- 只注册已开启分组的 API，配置更新时用 ``sync_dynamic_apis`` 整体替换，关闭的分组立即下线；
- 参数原样透传给 goqbot 同名动作，返回 ``{success, retcode, data, message}``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

import asyncio

from .codec import OutboundPlan
from .transport import TransportNotConnected

API_PREFIX = "adapter.goqbot."


@dataclass(frozen=True)
class ApiSpec:
    group: str  # ApiConfig 上的开关属性名
    name: str  # 不含前缀的 API 名
    action: str  # goqbot 动作名；为空表示自定义处理
    description: str


API_SPECS: Tuple[ApiSpec, ...] = (
    # 消息发送
    ApiSpec("message_enabled", "message.send", "",
            "发送 goqbot 消息段：message_type=group|private，target_id，message（段数组或文本），"
            "mode=auto|passive|active|wakeup（auto 按插件主动消息开关处理）"),
    # 素材库
    ApiSpec("media_enabled", "media.upload_image", "upload_media_image", "上传图片到素材库（file / url，name，force）"),
    ApiSpec("media_enabled", "media.get_image", "get_media_image", "查询素材（image_id / file_name）"),
    ApiSpec("media_enabled", "media.list_images", "get_media_images", "分页列出素材（cursor）"),
    ApiSpec("media_enabled", "media.delete_image", "delete_media_image", "删除素材（image_id）"),
    # 菜单与指令面板
    ApiSpec("panel_enabled", "panel.get_menu", "get_menu", "读取机器人快捷菜单"),
    ApiSpec("panel_enabled", "panel.set_menu", "set_menu", "设置机器人快捷菜单（items）"),
    ApiSpec("panel_enabled", "panel.list", "get_command_panels", "列出指令面板（scope，cursor，limit）"),
    ApiSpec("panel_enabled", "panel.get", "get_command_panel", "读取指令面板（panel_id）"),
    ApiSpec("panel_enabled", "panel.create", "create_command_panel", "创建指令面板"),
    ApiSpec("panel_enabled", "panel.update", "set_command_panel", "修改指令面板（panel_id）"),
    ApiSpec("panel_enabled", "panel.delete", "delete_command_panel", "删除指令面板（panel_id）"),
    ApiSpec("panel_enabled", "panel.set_target", "set_command_panel_target", "设置指令面板生效范围（panel_id）"),
    # 状态查询
    ApiSpec("query_enabled", "query.c2c_msg_state", "get_c2c_msg_state", "私聊用户是否允许主动推送（user_id / user_openid）"),
    ApiSpec("query_enabled", "query.group_bot_state", "get_group_bot_state", "机器人在群内的状态（group_id）"),
    ApiSpec("query_enabled", "query.openid", "get_openid", "数字 ID 换 openid（user_id / group_id）"),
    ApiSpec("query_enabled", "query.group_ban", "get_group_ban", "群禁言列表（group_id）"),
    ApiSpec("query_enabled", "query.group_member", "get_group_member_info", "群成员信息（group_id，user_id）"),
    ApiSpec("query_enabled", "query.group_members", "get_group_member_list", "群成员列表（group_id，cursor）"),
    ApiSpec("query_enabled", "query.group_join_requests", "get_group_join_requests", "进群申请列表（cursor，limit）"),
    # 群管理
    ApiSpec("group_admin_enabled", "group.set_ban", "set_group_ban", "禁言群成员（group_id，user_id，duration 秒，0=解除）"),
    ApiSpec("group_admin_enabled", "group.handle_join_request", "set_group_add_request",
            "处理进群申请（join_request_id / flag，approve，reason）"),
    # 通用透传
    ApiSpec("raw_enabled", "raw.call_action", "", "调用任意 goqbot 动作：action，params（字典）"),
)

API_GROUPS: Tuple[str, ...] = tuple(dict.fromkeys(spec.group for spec in API_SPECS))

_MESSAGE_ACTIONS = {
    ("group", "passive"): ("send_group_msg", {"passive_only": True}),
    ("group", "active"): ("send_group_active_msg", {}),
    ("private", "passive"): ("send_private_msg", {"passive_only": True}),
    ("private", "active"): ("send_private_active_msg", {}),
    ("private", "wakeup"): ("send_private_msg", {"is_wakeup": True}),
}


def _clean_params(kwargs: Mapping[str, Any]) -> Dict[str, Any]:
    """去掉 Host 注入的 ``__maibot_*`` 字段与值为 None 的参数。"""

    return {k: v for k, v in kwargs.items() if not k.startswith("__") and v is not None}


def _wrap(response: Mapping[str, Any]) -> Dict[str, Any]:
    ok = str(response.get("status") or "").lower() == "ok" and response.get("retcode", 0) == 0
    return {
        "success": ok,
        "retcode": response.get("retcode"),
        "data": response.get("data"),
        "message": str(response.get("wording") or response.get("message") or response.get("msg") or ""),
    }


def _error(message: str) -> Dict[str, Any]:
    return {"success": False, "retcode": None, "data": None, "message": message}


class GoqbotApisMixin:
    """开放接口实现；宿主类需提供 MaiBotPlugin 的动态 API 方法与 _call_action / _sender / _settings。"""

    def _rebuild_dynamic_apis(self) -> List[str]:
        """按当前配置重建动态 API 集合，返回已注册的 API 名。"""

        self.clear_dynamic_apis()  # type: ignore[attr-defined]
        api_config = self._settings().api  # type: ignore[attr-defined]
        names: List[str] = []
        for spec in API_SPECS:
            if not getattr(api_config, spec.group):
                continue
            full_name = API_PREFIX + spec.name
            self.register_dynamic_api(  # type: ignore[attr-defined]
                full_name,
                self._make_api_handler(spec),
                description=spec.description,
                version="1",
                public=True,
                handler_name=f"goqbot_api__{spec.name.replace('.', '__')}",
            )
            names.append(full_name)
        return names

    def _make_api_handler(self, spec: ApiSpec) -> Callable[..., Awaitable[Dict[str, Any]]]:
        async def handler(**kwargs: Any) -> Dict[str, Any]:
            if not getattr(self._settings().api, spec.group):  # type: ignore[attr-defined]
                return _error("该接口已在 goqbot 适配器配置中关闭")
            params = _clean_params(kwargs)
            try:
                if spec.name == "message.send":
                    return await self._api_send_message(params)
                if spec.name == "raw.call_action":
                    action = str(params.get("action") or "").strip()
                    if not action:
                        return _error("缺少 action")
                    raw_params = params.get("params") or {}
                    if not isinstance(raw_params, Mapping):
                        return _error("params 必须是字典")
                    return _wrap(await self._call_action(action, dict(raw_params)))  # type: ignore[attr-defined]
                return _wrap(await self._call_action(spec.action, params))  # type: ignore[attr-defined]
            except (TransportNotConnected, asyncio.TimeoutError) as exc:
                return _error(f"goqbot 未响应: {exc or '超时'}")

        handler.__name__ = f"goqbot_api_{spec.name.replace('.', '_')}"
        return handler

    async def _api_send_message(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        message_type = str(params.get("message_type") or "").strip().lower()
        if message_type not in {"group", "private"}:
            return _error("message_type 只能是 group 或 private")
        try:
            target_id = int(str(params.get("target_id") or "").strip())
        except ValueError:
            return _error("target_id 无效")
        message = params.get("message")
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        if not isinstance(message, list) or not message:
            return _error("message 必须是非空的消息段数组或文本")
        mode = str(params.get("mode") or "auto").strip().lower()
        id_key = "group_id" if message_type == "group" else "user_id"

        if mode == "auto":
            plan = OutboundPlan(chat_type=message_type, target_id=target_id, segments=message, dropped_types=[])
            result = await self._sender.send(plan, self._settings().active)  # type: ignore[attr-defined]
            return {
                "success": result.success,
                "retcode": result.retcode,
                "data": {"message_id": result.message_id, "mode": result.mode} if result.success else None,
                "message": result.error,
            }
        entry: Optional[Tuple[str, Dict[str, Any]]] = _MESSAGE_ACTIONS.get((message_type, mode))
        if entry is None:
            return _error(f"不支持的 mode: {mode}（wakeup 仅限私聊）")
        action, extra = entry
        response = await self._call_action(action, {id_key: target_id, "message": message, **extra})  # type: ignore[attr-defined]
        return _wrap(response)
