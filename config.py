"""goqbot 适配器配置模型（同时决定 WebUI 配置页的展示）。

配置页说明：
- 每个配置类的 docstring 会显示为该分组的说明文字；
- 字段的 ``hint`` 显示在输入框下方，``order`` 决定顺序；
- 配置页字段按两列网格排列（窄屏为单列），阅读顺序为从左到右、从上到下；
- 下拉框直接显示取值，所以「连接方式」的取值就是中文「正向 / 反向」；
- 当前 WebUI 尚未实现字段的条件显示（depends_on），所以依赖关系都写在说明里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Literal, Mapping
from urllib.parse import urlparse

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator

from .constants import (
    DEFAULT_ACTION_TIMEOUT_SEC,
    DEFAULT_ADDRESS,
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_PLATFORM,
    DEFAULT_RECONNECT_DELAY_SEC,
    MODE_FORWARD,
    MODE_REVERSE,
    RESERVED_PLATFORMS,
    SUPPORTED_CONFIG_VERSION,
)

ConnectionMode = Literal["正向", "反向"]

_MODE_ALIASES = {
    "forward": MODE_FORWARD,
    "正向": MODE_FORWARD,
    "reverse": MODE_REVERSE,
    "反向": MODE_REVERSE,
}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


class PluginOptions(PluginConfigBase):
    """插件总开关与本连接在 MaiBot 中的身份。"""

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
    platform: str = Field(
        default=DEFAULT_PLATFORM,
        description="连接标识：本连接在 MaiBot 中使用的平台名。",
        json_schema_extra={
            "label": "连接标识",
            "hint": (
                "默认 qq。同一个 MaiBot 接入多个协议端（如另一个 goqbot、NapCat）时，"
                "为每个协议端填写不同的标识（如 qq_official）。修改后视为新平台，原有聊天记录不会迁移。"
            ),
            "placeholder": DEFAULT_PLATFORM,
            "order": 1,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置结构版本。",
        json_schema_extra={"label": "配置版本", "hidden": True, "disabled": True, "order": 99},
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


@dataclass(frozen=True)
class ParsedAddress:
    scheme: str
    host: str
    port: int
    path: str


class ConnectionConfig(PluginConfigBase):
    """配置 goqbot 与 maibot 之间的连接方式"""

    __ui_label__: ClassVar[str] = "连接"
    __ui_icon__: ClassVar[str] = "plug"
    __ui_order__: ClassVar[int] = 1

    mode: ConnectionMode = Field(
        default=MODE_FORWARD,
        description="连接方式。",
        json_schema_extra={
            "label": "连接方式",
            "hint": "正向：插件连接 goqbot；反向：goqbot 连接插件。",
            "order": 0,
        },
    )
    address: str = Field(
        default=DEFAULT_ADDRESS,
        description="WebSocket 完整地址。",
        json_schema_extra={
            "label": "地址",
            "hint": "需要根据连接方式选择对应的编写格式，参考文档当中示例",
            "placeholder": DEFAULT_ADDRESS,
            "order": 1,
        },
    )
    access_token: str = Field(
        default="",
        description="访问令牌，需与 goqbot 的 onebot.access_token 一致。",
        json_schema_extra={
            "label": "访问令牌",
            "hint": "与 goqbot 的 onebot.access_token 保持一致；留空表示不鉴权（反向监听非本机地址时请务必设置）。",
            "x-widget": "password",
            "placeholder": "留空则不鉴权",
            "order": 2,
        },
    )
    reconnect_delay_sec: float = Field(
        default=DEFAULT_RECONNECT_DELAY_SEC,
        ge=1,
        le=3600,
        description="连接断开后的重连等待时间（秒）。",
        json_schema_extra={
            "label": "重连等待（秒）",
            "hint": "仅正向生效；反向的重连间隔由 goqbot 的 reconnect-interval 决定。",
            "order": 3,
            "step": 1,
        },
    )
    heartbeat_interval_sec: float = Field(
        default=DEFAULT_HEARTBEAT_INTERVAL_SEC,
        ge=5,
        le=3600,
        description="WebSocket 心跳保活间隔（秒）。",
        json_schema_extra={
            "label": "心跳间隔（秒）",
            "hint": "定时发送 WebSocket ping，及时发现已经失效的连接。",
            "order": 4,
            "step": 5,
        },
    )
    action_timeout_sec: float = Field(
        default=DEFAULT_ACTION_TIMEOUT_SEC,
        ge=1,
        le=600,
        description="调用 goqbot 动作的超时时间（秒）。",
        json_schema_extra={
            "label": "动作超时（秒）",
            "hint": "发消息、查询等动作超过该时间未响应即判定失败。",
            "order": 5,
            "step": 1,
        },
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        return _MODE_ALIASES.get(_text(value).lower(), _MODE_ALIASES.get(_text(value), MODE_FORWARD))

    @field_validator("address", "access_token", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _text(value)

    @property
    def is_reverse(self) -> bool:
        return self.mode == MODE_REVERSE

    def parse_address(self) -> ParsedAddress:
        """解析「地址」；不合法时抛出 ``ValueError``（错误信息可直接展示给用户）。"""

        parsed = urlparse(self.address)
        example = "ws://127.0.0.1:8095/onebot/v11/ws" if self.is_reverse else DEFAULT_ADDRESS
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError(f"地址无效: {self.address!r}（应形如 {example}）")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"地址端口无效: {self.address!r}") from exc
        if self.is_reverse:
            if parsed.scheme != "ws":
                raise ValueError("反向监听不支持 wss://，请使用 ws://（如需 TLS 请在前面加反向代理）")
            if port is None:
                raise ValueError(f"反向监听地址需要写明端口: {self.address!r}（应形如 {example}）")
        if port is None:
            port = 443 if parsed.scheme == "wss" else 80
        return ParsedAddress(parsed.scheme, parsed.hostname, port, parsed.path or "/")

    def address_error(self) -> str:
        try:
            self.parse_address()
        except ValueError as exc:
            return str(exc)
        return ""


class ActiveMessageConfig(PluginConfigBase):
    """
    主动消息（没有可用被动回复凭证时的发送）。
    群聊每条消息 5 分钟内最多回复 5 次，私聊 60 分钟内最多 4 次。群聊需群主开启「机器人主动在群聊内发言」的开关；
    官方频控为机器人维度 60 条/分钟（未认证 30 条/分钟）、单群每天最多 1000 条，超额直接发送失败。
    """

    __ui_label__: ClassVar[str] = "主动消息"
    __ui_icon__: ClassVar[str] = "send"
    __ui_order__: ClassVar[int] = 2

    private_enabled: bool = Field(
        default=False,
        description="私聊：无被动凭证时是否改发主动消息。",
        json_schema_extra={
            "label": "私聊主动消息",
            "hint": "默认关闭，与群聊开关相互独立。私聊主动推送额度极少，且用户可在 QQ 中关闭推送。",
            "order": 0,
        },
    )
    group_enabled: bool = Field(
        default=False,
        description="群聊：无被动凭证时是否改发主动消息。",
        json_schema_extra={
            "label": "群聊主动消息",
            "hint": "默认关闭。开启后受下方「单群频率限制」约束；关闭时下方两项不生效。",
            "order": 1,
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
            "order": 2,
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
            "order": 3,
            "step": 10,
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


class ToolsConfig(PluginConfigBase):
    """
    聊天工具：让 MaiBot 的模型在聊天中调用 goqbot 特有的能力。每项单独开关，默认全部关闭，保存后立即生效。
    工具只在本连接（连接标识）的会话中出现；修改「连接标识」后需重载插件，工具的平台范围才会更新。
    """

    __ui_label__: ClassVar[str] = "聊天工具"
    __ui_icon__: ClassVar[str] = "wrench"
    __ui_order__: ClassVar[int] = 4

    markdown_enabled: bool = Field(
        default=False,
        description="Markdown 消息工具（goqbot_send_markdown）。",
        json_schema_extra={
            "label": "Markdown 消息",
            "hint": "模型可发送原生 Markdown，并可附带跳转 / 指令按钮。发送同样遵循「主动消息」开关。",
            "order": 0,
        },
    )
    card_enabled: bool = Field(
        default=False,
        description="图文卡片工具（goqbot_send_card）。",
        json_schema_extra={
            "label": "图文卡片",
            "hint": "模型可发送带标题、简介、封面图的链接卡片；链接与图片必须是 https。发送同样遵循「主动消息」开关。",
            "order": 1,
        },
    )
    wakeup_enabled: bool = Field(
        default=False,
        description="互动召回工具（goqbot_send_wakeup），仅私聊。",
        json_schema_extra={
            "label": "互动召回",
            "hint": (
                "模型可给当前私聊的用户发互动召回消息，只能召回当前聊天的用户。用户最后一次私聊后 30 天内分 4 个周期"
                "（当天 / 1–3 天 / 3–7 天 / 7–30 天），每个周期 1 条。不受「主动消息」开关影响。"
            ),
            "order": 2,
        },
    )
    wakeup_block_chat_trigger: bool = Field(
        default=True,
        description="禁止通过聊天触发互动召回。",
        json_schema_extra={
            "label": "禁止聊天触发召回",
            "hint": (
                "默认开启：用户最近 60 分钟内在该私聊发过消息时拒绝召回，召回只能由 AI 在用户长时间未互动时自行发起，"
                "用户无法通过聊天让机器人发召回消息（60 分钟内普通回复本就可用）。"
            ),
            "order": 3,
        },
    )
    push_state_enabled: bool = Field(
        default=False,
        description="主动消息权限查询工具（goqbot_get_push_state）。",
        json_schema_extra={
            "label": "主动消息权限查询",
            "hint": "模型可查询当前群 / 私聊是否允许机器人主动发消息，以及插件的主动消息开关状态。",
            "order": 4,
        },
    )
    recall_enabled: bool = Field(
        default=False,
        description="撤回工具（goqbot_recall_message）。",
        json_schema_extra={
            "label": "撤回消息",
            "hint": "模型可撤回机器人自己发过的消息（QQ 官方只允许撤回机器人自己的消息，且有时间限制）。",
            "order": 5,
        },
    )
    mute_enabled: bool = Field(
        default=False,
        description="禁言工具（goqbot_mute_member），仅群聊。",
        json_schema_extra={
            "label": "群成员禁言",
            "hint": "执行「允许触发的用户」发出的禁言 / 解禁指令（格式见「禁言工具」分组），模型不能自行禁言任何人。需要机器人是群管理员。",
            "order": 6,
        },
    )


def _id_list(value: Any) -> list:
    """规范化 ID 列表：去空白、去重，兼容 ``qq:123`` 写法。"""

    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else str(value).replace("，", ",").split(",")
    result: list = []
    for item in items:
        text = _text(item)
        if ":" in text:
            text = text.rsplit(":", 1)[1].strip()
        if text and text not in result:
            result.append(text)
    return result


class MuteConfig(PluginConfigBase):
    """
    禁言工具的安全限制（需先在「聊天工具」中开启「群成员禁言」）。
    只执行「允许触发的用户」本人发出的指令：「禁言 @某人 时长」或「解禁 @某人」（时长如 30秒 / 10分钟 / 2小时 / 1天，纯数字按分钟，不写默认 10 分钟）。
    禁言对象和时长只从指令原文解析，模型无法指定；指令需在 10 分钟内、且只能执行一次。名单留空时禁言工具不会执行任何操作。
    名单填 goqbot 下发的数字 user_id / group_id；同一个人在私聊和不同群中的 user_id 可能不同，请按该群聊天记录中显示的 ID 填写。
    @ 成员需要 goqbot 设置 qq.mention_mode: at。
    """

    __ui_label__: ClassVar[str] = "禁言工具"
    __ui_icon__: ClassVar[str] = "shield"
    __ui_order__: ClassVar[int] = 5

    trigger_users: list[str] = Field(
        default_factory=list,
        description="允许通过聊天触发禁言的用户（白名单）。",
        json_schema_extra={
            "label": "允许触发的用户",
            "hint": "只有名单内用户本人发出的禁言 / 解禁指令会被执行；留空表示任何人都无法触发禁言。",
            "order": 0,
        },
    )
    protected_users: list[str] = Field(
        default_factory=list,
        description="禁言保护名单：名单里的人不会被禁言。",
        json_schema_extra={
            "label": "禁言保护名单",
            "hint": "名单内的用户永远不会被禁言（解除禁言不受影响）。建议把群主、管理员加入。",
            "order": 1,
        },
    )
    allowed_groups: list[str] = Field(
        default_factory=list,
        description="允许禁言的群，留空表示所有群都可用。",
        json_schema_extra={
            "label": "允许禁言的群",
            "hint": "填写 group_id；留空表示所有群都可用。",
            "order": 2,
        },
    )
    max_sec: int = Field(
        default=3600,
        ge=60,
        le=2592000,
        description="禁言工具允许的最长禁言时长（秒）。",
        json_schema_extra={
            "label": "最长禁言时长（秒）",
            "hint": "指令中的时长超过该值时按该值禁言。",
            "order": 3,
            "step": 60,
        },
    )

    @field_validator("trigger_users", "protected_users", "allowed_groups", mode="before")
    @classmethod
    def _normalize_ids(cls, value: Any) -> list:
        return _id_list(value)


class ApiConfig(PluginConfigBase):
    """
    开放接口：供其他 MaiBot 插件通过 ctx.api.call 调用的 goqbot 接口，API 名均以 adapter.goqbot. 开头。
    每组单独开关，默认全部关闭；关闭的接口会立即下线。参数与返回值与 goqbot 同名动作一致，详见 README。
    """

    __ui_label__: ClassVar[str] = "开放接口"
    __ui_icon__: ClassVar[str] = "blocks"
    __ui_order__: ClassVar[int] = 6

    message_enabled: bool = Field(
        default=False,
        description="消息发送接口（adapter.goqbot.message.*）。",
        json_schema_extra={
            "label": "消息发送",
            "hint": "发送任意 goqbot 消息段（Markdown、按钮、卡片、流式等），可选被动 / 主动 / 互动召回 / 自动。",
            "order": 0,
        },
    )
    media_enabled: bool = Field(
        default=False,
        description="素材库接口（adapter.goqbot.media.*）。",
        json_schema_extra={
            "label": "素材库",
            "hint": "上传、查询、删除 goqbot 素材库中的图片（在 Markdown 中用 qqbot-media:// 引用）。",
            "order": 1,
        },
    )
    panel_enabled: bool = Field(
        default=False,
        description="菜单与指令面板接口（adapter.goqbot.panel.*）。",
        json_schema_extra={
            "label": "菜单与指令面板",
            "hint": "读取 / 设置机器人快捷菜单，增删改指令面板。",
            "order": 2,
        },
    )
    query_enabled: bool = Field(
        default=False,
        description="状态查询接口（adapter.goqbot.query.*）。",
        json_schema_extra={
            "label": "状态查询",
            "hint": "查询私聊推送权限、机器人在群内的状态、openid、群禁言列表、群成员与进群申请等。",
            "order": 3,
        },
    )
    group_admin_enabled: bool = Field(
        default=False,
        description="群管理接口（adapter.goqbot.group.*）。",
        json_schema_extra={
            "label": "群管理",
            "hint": "禁言 / 解除禁言群成员、处理进群申请。需要机器人在群内具有管理权限。",
            "order": 4,
        },
    )
    raw_enabled: bool = Field(
        default=False,
        description="通用动作透传接口（adapter.goqbot.raw.call_action）。",
        json_schema_extra={
            "label": "通用动作透传",
            "hint": "允许其他插件直接调用任意 goqbot 动作，权限最大，仅在信任所有已安装插件时开启。",
            "order": 5,
        },
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



def _migrate_v1_0(raw: Dict[str, Any]) -> None:
    """把 0.1.0（配置版本 1.0.0）的连接配置迁移到当前结构。

    旧结构：connection.platform / forward_url / reverse_host / reverse_port / reverse_path，
    mode 取 forward / reverse。SDK 会在校验前用默认值补齐缺失字段，所以这里以
    「字段仍是默认值」判断用户是否已在新结构中填写过。
    """

    conn = raw.get("connection")
    plugin = raw.get("plugin")
    if not isinstance(conn, dict):
        return
    if not isinstance(plugin, dict):
        plugin = raw["plugin"] = {}

    legacy_platform = _text(conn.pop("platform", ""))
    if legacy_platform and _text(plugin.get("platform")) in {"", DEFAULT_PLATFORM}:
        plugin["platform"] = legacy_platform

    legacy_keys = ("forward_url", "reverse_host", "reverse_port", "reverse_path")
    if not any(key in conn for key in legacy_keys):
        return
    forward_url = _text(conn.pop("forward_url", ""))
    host = _text(conn.pop("reverse_host", "")) or "127.0.0.1"
    port = _text(conn.pop("reverse_port", "")) or "8095"
    path = _text(conn.pop("reverse_path", "")) or "/onebot/v11/ws"
    if not path.startswith("/"):
        path = f"/{path}"
    if _text(conn.get("address")) not in {"", DEFAULT_ADDRESS}:
        return
    mode = _MODE_ALIASES.get(_text(conn.get("mode")).lower(), MODE_FORWARD)
    if mode == MODE_REVERSE:
        conn["address"] = f"ws://{host}:{port}{path}"
    elif forward_url:
        conn["address"] = forward_url


def _migrate_v0_2_0(raw: Dict[str, Any]) -> None:
    """0.2.0 的 tools.mute_max_sec 移到 0.2.1 新增的「禁言工具」分组（mute.max_sec）。"""

    tools = raw.get("tools")
    if not isinstance(tools, dict) or "mute_max_sec" not in tools:
        return
    legacy = tools.pop("mute_max_sec")
    mute = raw.get("mute")
    if not isinstance(mute, dict):
        mute = raw["mute"] = {}
    if "max_sec" not in mute or mute.get("max_sec") in (None, 3600):
        mute["max_sec"] = legacy


class GoqbotAdapterSettings(PluginConfigBase):
    """goqbot 适配器完整配置。"""

    plugin: PluginOptions = Field(default_factory=PluginOptions)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    active: ActiveMessageConfig = Field(default_factory=ActiveMessageConfig)
    message: MessageConfig = Field(default_factory=MessageConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mute: MuteConfig = Field(default_factory=MuteConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, raw: Any) -> Any:
        if not isinstance(raw, Mapping):
            return raw
        data: Dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in raw.items()}
        _migrate_v1_0(data)
        _migrate_v0_2_0(data)
        plugin = data.get("plugin")
        if isinstance(plugin, dict) and _text(plugin.get("config_version")) in {"1.0.0"}:
            plugin["config_version"] = SUPPORTED_CONFIG_VERSION
        return data

    def runtime_config_error(self) -> str:
        """返回阻止建立连接的配置错误；无错误时返回空串。"""

        if self.plugin.config_version != SUPPORTED_CONFIG_VERSION:
            return (
                f"配置版本不兼容：当前 {self.plugin.config_version!r}，插件要求 {SUPPORTED_CONFIG_VERSION!r}"
            )
        return self.connection.address_error()
