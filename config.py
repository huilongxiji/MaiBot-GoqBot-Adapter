"""goqbot 适配器配置模型（同时决定 WebUI 配置页的展示）。

配置页说明：
- 每个配置类的 docstring 会显示为该分组的说明文字；
- 字段的 ``hint`` 显示在输入框下方；
- 当前 WebUI 尚未实现字段的条件显示（depends_on），所以依赖关系都写在说明里。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

from .constants import (
    DEFAULT_ACTION_TIMEOUT_SEC,
    DEFAULT_FORWARD_URL,
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_PLATFORM,
    DEFAULT_RECONNECT_DELAY_SEC,
    DEFAULT_REVERSE_HOST,
    DEFAULT_REVERSE_PATH,
    DEFAULT_REVERSE_PORT,
    RESERVED_PLATFORMS,
    SUPPORTED_CONFIG_VERSION,
)

ConnectionMode = Literal["forward", "reverse"]


def _text(value: Any) -> str:
    return str(value or "").strip()


class PluginOptions(PluginConfigBase):
    """插件总开关。启用后按「连接」分组的设置连接 goqbot。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用 goqbot 适配器。",
        json_schema_extra={
            "label": "启用适配器",
            "hint": "关闭后插件保持空闲，不会建立或监听任何 WebSocket 连接。",
            "order": 0,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本。",
        json_schema_extra={"label": "配置版本", "hidden": True, "disabled": True, "order": 99},
    )


class ConnectionConfig(PluginConfigBase):
    """与 goqbot 的 OneBot V11 WebSocket 连接。

    正向：插件作为客户端，连接 goqbot 配置里 onebot.servers 的 ws（例如 0.0.0.0:6700）。
    反向：插件作为服务端监听端口，在 goqbot 的 onebot.servers 里添加一条 ws-reverse 指向这里。
    两种方式二选一；goqbot 的其他下游（如 NoneBot）不受影响，所有下游都会收到全部事件。
    """

    __ui_label__: ClassVar[str] = "连接"
    __ui_icon__: ClassVar[str] = "plug"
    __ui_order__: ClassVar[int] = 1

    platform: str = Field(
        default=DEFAULT_PLATFORM,
        description="连接标识：本连接在 MaiBot 中使用的平台名。",
        json_schema_extra={
            "label": "连接标识",
            "hint": (
                "默认 qq。同一个 MaiBot 同时接入多个协议端（例如另一个 goqbot、NapCat）时，"
                "给每个协议端填写不同的标识（如 qq_official），避免用户/群号在 MaiBot 中混在一起。"
                "修改后 MaiBot 会把它当作一个新平台，原有的聊天记录与人物信息不会自动迁移。"
            ),
            "placeholder": "qq",
            "order": 0,
        },
    )
    mode: ConnectionMode = Field(
        default="forward",
        description="连接方式：forward=正向（插件连 goqbot），reverse=反向（goqbot 连插件）。",
        json_schema_extra={
            "label": "连接方式",
            "hint": "forward：填写下方「正向地址」；reverse：填写下方「反向监听」三项，并在 goqbot 中配置 ws-reverse。",
            "order": 1,
        },
    )
    forward_url: str = Field(
        default=DEFAULT_FORWARD_URL,
        description="正向 WebSocket 地址（仅 forward 模式使用）。",
        json_schema_extra={
            "label": "正向地址",
            "hint": "goqbot 正向 ws 的地址，例如 ws://127.0.0.1:6700。仅在连接方式为 forward 时生效。",
            "placeholder": DEFAULT_FORWARD_URL,
            "order": 2,
        },
    )
    reverse_host: str = Field(
        default=DEFAULT_REVERSE_HOST,
        description="反向 WebSocket 监听地址（仅 reverse 模式使用）。",
        json_schema_extra={
            "label": "反向监听地址",
            "hint": "goqbot 与 MaiBot 在同一台机器时保持 127.0.0.1；跨机器/容器时改为 0.0.0.0 并务必设置访问令牌。",
            "placeholder": DEFAULT_REVERSE_HOST,
            "order": 3,
        },
    )
    reverse_port: int = Field(
        default=DEFAULT_REVERSE_PORT,
        ge=1,
        le=65535,
        description="反向 WebSocket 监听端口（仅 reverse 模式使用）。",
        json_schema_extra={"label": "反向监听端口", "order": 4, "step": 1},
    )
    reverse_path: str = Field(
        default=DEFAULT_REVERSE_PATH,
        description="反向 WebSocket 路径（仅 reverse 模式使用）。",
        json_schema_extra={
            "label": "反向路径",
            "hint": (
                "goqbot 中对应写法：ws-reverse.universal: ws://<监听地址>:<端口><路径>，"
                "例如 ws://127.0.0.1:8095/onebot/v11/ws。"
            ),
            "placeholder": DEFAULT_REVERSE_PATH,
            "order": 5,
        },
    )
    access_token: str = Field(
        default="",
        description="访问令牌，需与 goqbot 的 onebot.access_token 一致；为空表示不鉴权。",
        json_schema_extra={
            "label": "访问令牌",
            "hint": "与 goqbot 配置 onebot.access_token 保持一致。正向时插件携带它，反向时插件用它校验 goqbot。",
            "input_type": "password",
            "placeholder": "可留空",
            "order": 6,
        },
    )
    reconnect_delay_sec: float = Field(
        default=DEFAULT_RECONNECT_DELAY_SEC,
        gt=0,
        description="正向连接断开后的重连等待（秒）。",
        json_schema_extra={"label": "重连等待（秒）", "order": 7, "step": 1},
    )
    heartbeat_interval_sec: float = Field(
        default=DEFAULT_HEARTBEAT_INTERVAL_SEC,
        gt=0,
        description="WebSocket 协议层 ping 保活间隔（秒）。",
        json_schema_extra={"label": "心跳间隔（秒）", "order": 8, "step": 1},
    )
    action_timeout_sec: float = Field(
        default=DEFAULT_ACTION_TIMEOUT_SEC,
        gt=0,
        description="调用 goqbot 动作（发消息、查询等）的超时（秒）。",
        json_schema_extra={"label": "动作超时（秒）", "order": 9, "step": 1},
    )

    @field_validator("platform", mode="before")
    @classmethod
    def _normalize_platform(cls, value: Any) -> str:
        platform = _text(value).lower()
        if not platform:
            return DEFAULT_PLATFORM
        if platform in RESERVED_PLATFORMS:
            raise ValueError(f"连接标识不能使用 MaiBot 保留名称: {platform}")
        return platform

    @field_validator("forward_url", "reverse_host", "access_token", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _text(value)

    @field_validator("reverse_path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> str:
        path = _text(value) or DEFAULT_REVERSE_PATH
        return path if path.startswith("/") else f"/{path}"

    def forward_url_error(self) -> str:
        """校验正向地址，返回错误描述；合法时返回空串。"""

        parsed = urlparse(self.forward_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            return f"正向地址无效: {self.forward_url!r}（应形如 ws://127.0.0.1:6700）"
        return ""


class ActiveMessageConfig(PluginConfigBase):
    """主动消息（没有可用被动回复凭证时的发送）。

    QQ 官方机器人回复分两种：
    · 被动回复：针对用户刚发的消息回复。群聊每条消息 5 分钟内最多回复 5 次，私聊 60 分钟内最多 4 次。
      这份额度由 goqbot 的所有下游（NoneBot、MaiBot 等）共用。
    · 主动消息：不依赖用户消息直接发送。群聊需群主开启「机器人在群聊内发言」；
      官方频控为机器人维度 60 条/分钟（未认证 30 条/分钟）、单群每天最多 1000 条，超额直接发送失败。
      私聊主动推送需用户未关闭推送，且额度极少。

    插件发送时总是先尝试被动回复（要求 goqbot >= 1.11.0 的 passive_only）。
    只有被动凭证用完或过期时，才按下面的开关决定是否改发主动消息；关闭时该条消息直接丢弃并记录日志。
    """

    __ui_label__: ClassVar[str] = "主动消息"
    __ui_icon__: ClassVar[str] = "send"
    __ui_order__: ClassVar[int] = 2

    group_enabled: bool = Field(
        default=False,
        description="群聊：无被动凭证时是否改发主动消息。",
        json_schema_extra={
            "label": "群聊主动消息",
            "hint": "默认关闭。开启后受下方「单群频率限制」约束；关闭时下方两项不生效。",
            "order": 0,
        },
    )
    group_max_per_window: int = Field(
        default=5,
        ge=1,
        le=1000,
        description="单群频率限制：时间窗口内每个群最多发送的主动消息条数。",
        json_schema_extra={
            "label": "单群最多条数",
            "hint": "仅在开启「群聊主动消息」时生效，只统计主动消息，被动回复不计入。",
            "order": 1,
            "step": 1,
        },
    )
    group_window_sec: int = Field(
        default=600,
        ge=10,
        le=86400,
        description="单群频率限制的时间窗口（秒）。",
        json_schema_extra={
            "label": "时间窗口（秒）",
            "hint": "例如 5 条 / 600 秒 表示每个群每 10 分钟最多 5 条主动消息，超出的消息直接丢弃。",
            "order": 2,
            "step": 10,
        },
    )
    private_enabled: bool = Field(
        default=False,
        description="私聊：无被动凭证时是否改发主动消息。",
        json_schema_extra={
            "label": "私聊主动消息",
            "hint": "默认关闭，与群聊开关相互独立。私聊主动推送额度极少，且用户可在 QQ 中关闭推送。",
            "order": 3,
        },
    )


class MessageConfig(PluginConfigBase):
    """消息收发细节。"""

    __ui_label__: ClassVar[str] = "消息"
    __ui_icon__: ClassVar[str] = "message-circle"
    __ui_order__: ClassVar[int] = 3

    download_images: bool = Field(
        default=True,
        description="是否下载入站图片交给 MaiBot 识图。",
        json_schema_extra={
            "label": "下载入站图片",
            "hint": "关闭后图片以 [图片] 文本形式传给 MaiBot。",
            "order": 0,
        },
    )
    image_max_mb: float = Field(
        default=10.0,
        gt=0,
        le=100,
        description="单张入站图片的下载上限（MB），超出则以 [图片] 文本代替。",
        json_schema_extra={"label": "图片大小上限（MB）", "order": 1, "step": 1},
    )


class DebugConfig(PluginConfigBase):
    """调试选项。"""

    __ui_label__: ClassVar[str] = "调试"
    __ui_icon__: ClassVar[str] = "bug"
    __ui_order__: ClassVar[int] = 9

    log_raw_events: bool = Field(
        default=False,
        description="以 DEBUG 级别打印 goqbot 原始事件与出站动作。",
        json_schema_extra={"label": "打印原始报文", "order": 0},
    )


class GoqbotAdapterSettings(PluginConfigBase):
    """goqbot 适配器完整配置。"""

    plugin: PluginOptions = Field(default_factory=PluginOptions)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    active: ActiveMessageConfig = Field(default_factory=ActiveMessageConfig)
    message: MessageConfig = Field(default_factory=MessageConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)

    def runtime_config_error(self) -> str:
        """返回阻止建立连接的配置错误；无错误时返回空串。"""

        if self.plugin.config_version != SUPPORTED_CONFIG_VERSION:
            return (
                f"配置版本不兼容：当前 {self.plugin.config_version!r}，插件要求 {SUPPORTED_CONFIG_VERSION!r}"
            )
        if self.connection.mode == "forward":
            return self.connection.forward_url_error()
        if not self.connection.reverse_host:
            return "反向监听地址为空"
        return ""
