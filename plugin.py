"""goqbot 适配器插件：把 MaiBot 接到 goqbot（QQ 官方机器人 ↔ OneBot V11 协议端）。

goqbot 本身支持同时向多个 OneBot 下游广播事件，本插件只是其中一个下游：
NoneBot 等其他框架可以和 MaiBot 同时连在同一个 goqbot 上，各自决定是否回复。

- 连接：正向（插件连 goqbot）或反向（goqbot 连插件）二选一。
- 入站：goqbot message 事件 → MaiBot 消息，全部接收不过滤（会话放行交给 MaiBot 的 adapter_policy）。
- 出站：先用 passive_only 尝试被动回复；没有被动凭证时按「主动消息」开关与单群频率限制处理。
- 聊天工具（tools.py）与开放接口（apis.py）：goqbot 特有能力，各自单独开关，默认关闭。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Tuple, cast

import asyncio
import contextlib

import aiohttp
from maibot_sdk import ConfigWidget, ConfigWidgetState, MaiBotPlugin, MessageGateway, PluginConfigBase

from .apis import GoqbotApisMixin
from .codec import InboundCodec, OutboundCodec
from .config import GoqbotAdapterSettings
from .constants import GATEWAY_NAME, GATEWAY_PROTOCOL, PASSIVE_ONLY_MIN_GOQBOT_VERSION
from .sender import OutboundSender
from .tools import TOOL_SWITCHES, GoqbotToolsMixin
from .transport import TransportNotConnected, _BaseTransport, build_transport


class _LazyLogger:
    """在 ctx 注入后才解析的 logger 代理（插件构造时 ctx 尚不可用）。"""

    def __init__(self, plugin: MaiBotPlugin) -> None:
        self._plugin = plugin

    def __getattr__(self, name: str) -> Any:
        return getattr(self._plugin.ctx.logger, name)


def _version_tuple(version: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for piece in str(version or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


class GoqbotAdapterPlugin(GoqbotToolsMixin, GoqbotApisMixin, MaiBotPlugin):
    """goqbot 双工消息网关。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = GoqbotAdapterSettings

    config_widgets: ClassVar[List[ConfigWidget]] = [
        ConfigWidget(id="connection", kind="status", label="goqbot 连接状态"),
        ConfigWidget(id="reconnect", kind="action", label="重新连接"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._transport: Optional[_BaseTransport] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._lifecycle_lock = asyncio.Lock()
        self._inbound = InboundCodec(self._load_image, self._resolve_group_name)
        self._outbound = OutboundCodec()
        self._sender = OutboundSender(self._call_action, _LazyLogger(self))
        self._account_id: Optional[str] = None
        self._platform: Optional[str] = None
        self._goqbot_version: str = ""
        self._group_names: Dict[str, str] = {}
        self._group_info_supported: Optional[bool] = None
        self._last_error: str = ""

    # ── 生命周期 ────────────────────────────────────────────────

    async def on_load(self) -> None:
        await self._sync_features()
        await self._restart()

    async def on_unload(self) -> None:
        async with self._lifecycle_lock:
            await self._stop()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        await self._sync_features()
        await self._restart()

    # ── 聊天工具 / 开放接口的开关 ────────────────────────────────

    def get_components(self) -> List[Dict[str, Any]]:
        """注册组件：工具按开关写入初始启用状态，并限定在本连接的平台；开放接口只注册已开启的分组。"""

        settings = self._settings()
        self._rebuild_dynamic_apis()
        components = super().get_components()
        for component in components:
            if component.get("type") != "TOOL" or component.get("name") not in TOOL_SWITCHES:
                continue
            metadata = dict(component.get("metadata") or {})
            metadata["enabled"] = bool(getattr(settings.tools, TOOL_SWITCHES[component["name"]]))
            metadata["platforms"] = [settings.plugin.platform]
            component["metadata"] = metadata
            # SDK 的 Tool 装饰器只把 chat_scope 放进 metadata，Host 读取的是组件顶层字段。
            component["chat_scope"] = str(metadata.get("chat_scope") or "all")
        return components

    async def _sync_features(self) -> None:
        """把「聊天工具」「开放接口」的开关同步到 MaiBot。"""

        tools = self._settings().tools
        for name, switch in TOOL_SWITCHES.items():
            enabled = bool(getattr(tools, switch))
            full_name = f"{self.ctx.plugin_id}.{name}"
            try:
                if enabled:
                    result = await self.ctx.component.enable_component(full_name, "TOOL")
                else:
                    result = await self.ctx.component.disable_component(full_name, "TOOL")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(f"[goqbot] 同步工具 {name} 开关失败: {exc}")
                continue
            if result is False or (isinstance(result, dict) and not result.get("success", False)):
                error = result.get("error") if isinstance(result, dict) else result
                self.ctx.logger.warning(f"[goqbot] 同步工具 {name} 开关失败: {error}")

        names = self._rebuild_dynamic_apis()
        try:
            accepted = await self.sync_dynamic_apis(offline_reason="该接口已在 goqbot 适配器配置中关闭")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"[goqbot] 同步开放接口失败: {exc}")
            return
        if not accepted:
            self.ctx.logger.warning("[goqbot] MaiBot 未接受开放接口更新")
        elif names:
            self.ctx.logger.debug(f"[goqbot] 已开放接口: {', '.join(names)}")

    # ── 配置页运行时控件 ────────────────────────────────────────

    async def get_config_widget_state(self) -> Dict[str, ConfigWidgetState]:
        if self._account_id:
            text = f"QQ {self._account_id}，goqbot 程序版本 {self._goqbot_version or '未知'}"
            tone = "success"
        elif self._transport is not None:
            text = f"等待 goqbot：{self._transport.describe()}"
            if self._last_error:
                text += f"（{self._last_error}）"
            tone = "warning"
        else:
            text = self._last_error or "未启用"
            tone = "error" if self._last_error else "neutral"
        return {
            "connection": ConfigWidgetState(text=text, tone=tone),
            "reconnect": ConfigWidgetState(disabled=not self._settings().plugin.enabled),
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id != "reconnect":
            raise ValueError(f"未知配置页操作: {action_id}")
        await self._restart()

    # ── 出站网关 ────────────────────────────────────────────────

    @MessageGateway(
        "duplex",
        name=GATEWAY_NAME,
        protocol=GATEWAY_PROTOCOL,
        description="goqbot（QQ 官方机器人）OneBot V11 双工消息网关",
    )
    async def handle_goqbot_gateway(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """把 MaiBot 的出站消息发到 goqbot。"""

        del metadata, kwargs
        internal_id = str(message.get("message_id") or "").strip()
        try:
            plan = self._outbound.build(message, route or {})
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if plan.dropped_types:
            self.ctx.logger.debug(f"[goqbot] 出站消息中以下段类型无法发送，已跳过: {plan.dropped_types}")
        if self._settings().debug.log_raw_events:
            self.ctx.logger.debug(f"[goqbot] 出站 {plan.chat_type}:{plan.target_id} {plan.segments}"[:2000])

        try:
            result = await self._sender.send(plan, self._settings().active)
        except (TransportNotConnected, asyncio.TimeoutError) as exc:
            return {"success": False, "error": f"goqbot 未响应: {exc or '超时'}"}
        except Exception as exc:  # noqa: BLE001 - 发送失败不应抛进 Host
            self.ctx.logger.warning(f"[goqbot] 发送异常: {exc}")
            return {"success": False, "error": f"发送异常: {exc}"}
        if not result.success:
            return {
                "success": False,
                "error": result.error,
                "metadata": {"action": result.action, "retcode": result.retcode, "mode": result.mode},
            }

        callbacks = []
        if internal_id and result.message_id and internal_id != result.message_id:
            callbacks.append(
                {
                    "name": "message_id_echo",
                    "payload": {"content": {"type": "echo", "echo": internal_id, "actual_id": result.message_id}},
                }
            )
        return {
            "success": True,
            "external_message_id": result.message_id or None,
            "metadata": {"action": result.action, "mode": result.mode, "adapter_callbacks": callbacks},
        }

    # ── 连接管理 ────────────────────────────────────────────────

    def _settings(self) -> GoqbotAdapterSettings:
        return cast(GoqbotAdapterSettings, self.config)

    async def _restart(self) -> None:
        async with self._lifecycle_lock:
            await self._stop()
            settings = self._settings()
            if not settings.plugin.enabled:
                self._last_error = ""
                self.ctx.logger.info("[goqbot] 适配器未启用，保持空闲")
                return
            if error := settings.runtime_config_error():
                self._last_error = error
                self.ctx.logger.error(f"[goqbot] 配置错误，未建立连接：{error}")
                return
            self._last_error = ""
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, connect=10))
            transport = build_transport(
                settings.connection, self.ctx.logger, self._on_open, self._on_close, self._on_event
            )
            transport.log_raw = settings.debug.log_raw_events
            self._transport = transport
            try:
                await transport.start()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self.ctx.logger.error(f"[goqbot] {exc}")
                await self._stop()

    async def _stop(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.stop()
        await self._report_disconnected()
        http, self._http = self._http, None
        if http is not None:
            with contextlib.suppress(Exception):
                await http.close()
        self._group_names.clear()
        self._group_info_supported = None

    async def _call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        transport = self._transport
        if transport is None:
            raise TransportNotConnected("适配器未启动")
        return await transport.call_action(action, params)

    async def _call_data(self, action: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        response = await self._call_action(action, params or {})
        if str(response.get("status") or "").lower() != "ok":
            return None
        data = response.get("data")
        return data if isinstance(data, dict) else None

    async def _on_open(self, self_id_hint: str) -> None:
        """连接建立：识别 goqbot、取得 self_id，然后让 MaiBot 网关就绪。"""

        settings = self._settings()
        version_info = await self._call_data("get_version_info") or {}
        app_name = str(version_info.get("app_name") or "")
        self._goqbot_version = str(version_info.get("app_version") or "")
        if app_name and app_name.lower() != "goqbot":
            self.ctx.logger.warning(f"[goqbot] 对端 app_name={app_name!r} 不是 goqbot，主动消息开关可能无法生效")
        elif _version_tuple(self._goqbot_version) < _version_tuple(PASSIVE_ONLY_MIN_GOQBOT_VERSION):
            self.ctx.logger.warning(
                f"[goqbot] goqbot 版本 {self._goqbot_version or '未知'} 低于 {PASSIVE_ONLY_MIN_GOQBOT_VERSION}，"
                "不支持 passive_only：无被动凭证时 goqbot 会自动改发主动消息，「主动消息」开关将无法拦截"
            )

        login = await self._call_data("get_login_info") or {}
        self_id = str(login.get("user_id") or self_id_hint or "").strip()
        if not self_id or self_id == "0":
            raise RuntimeError("无法从 goqbot 获取 self_id（get_login_info 未返回 user_id）")

        platform = settings.plugin.platform
        accepted = await self.ctx.gateway.update_state(
            GATEWAY_NAME,
            ready=True,
            platform=platform,
            account_id=self_id,
            metadata={"protocol": GATEWAY_PROTOCOL, "mode": settings.connection.mode,
                      "goqbot_version": self._goqbot_version},
        )
        if not accepted:
            raise RuntimeError("MaiBot 未接受网关就绪状态")
        self._account_id = self_id
        self._platform = platform
        nickname = str(login.get("nickname") or "")
        self.ctx.logger.info(
            f"[goqbot] 网关已就绪：platform={platform} self_id={self_id}"
            f"{f' 昵称={nickname}' if nickname else ''} goqbot={self._goqbot_version or '?'}"
        )

    async def _on_close(self) -> None:
        await self._report_disconnected()

    async def _report_disconnected(self) -> None:
        if self._account_id is None:
            return
        platform = self._platform or ""
        self._account_id = None
        self._platform = None
        with contextlib.suppress(Exception):
            await self.ctx.gateway.update_state(GATEWAY_NAME, ready=False, platform=platform)
        self.ctx.logger.info("[goqbot] 网关已下线")

    # ── 入站 ────────────────────────────────────────────────────

    async def _on_event(self, payload: Dict[str, Any]) -> None:
        post_type = str(payload.get("post_type") or "")
        if post_type != "message":
            return  # meta_event / notice / request：第一期不转发
        account_id = self._account_id
        platform = self._platform
        if account_id is None or platform is None:
            self.ctx.logger.debug("[goqbot] 网关尚未就绪，丢弃一条消息")
            return
        settings = self._settings()
        result = await self._inbound.build(
            payload, platform=platform, account_id=account_id, download_images=settings.message.download_images
        )
        if result is None:
            return
        accepted = await self.ctx.gateway.route_message(
            GATEWAY_NAME,
            result.message,
            external_message_id=result.external_message_id,
            dedupe_key=result.external_message_id,
        )
        if not accepted:
            self.ctx.logger.debug(f"[goqbot] MaiBot 未接收消息 {result.external_message_id}（可能被 adapter_policy 拦截）")

    async def _load_image(self, url: str) -> Optional[bytes]:
        http = self._http
        if http is None:
            return None
        limit = int(self._settings().message.image_max_mb * 1024 * 1024)
        try:
            async with http.get(url) as resp:
                if resp.status != 200:
                    self.ctx.logger.debug(f"[goqbot] 下载图片失败 HTTP {resp.status}")
                    return None
                if resp.content_length and resp.content_length > limit:
                    return None
                chunks: List[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > limit:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.ctx.logger.debug(f"[goqbot] 下载图片失败: {exc}")
            return None

    async def _resolve_group_name(self, group_id: str) -> str:
        """goqbot 群事件不带群名；get_group_info 是白名单接口，失败一次后不再尝试。"""

        if group_id in self._group_names:
            return self._group_names[group_id]
        name = ""
        if self._group_info_supported is not False:
            try:
                response = await self._call_action("get_group_info", {"group_id": int(group_id)})
            except (TransportNotConnected, asyncio.TimeoutError, ValueError):
                response = {}
            data = response.get("data") if str(response.get("status") or "").lower() == "ok" else None
            if isinstance(data, dict):
                self._group_info_supported = True
                name = str(data.get("group_name") or "").strip()
            elif response:
                self._group_info_supported = False
        self._group_names[group_id] = name
        return name


def create_plugin() -> GoqbotAdapterPlugin:
    return GoqbotAdapterPlugin()
