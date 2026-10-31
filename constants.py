"""goqbot 适配器共享常量。"""

PLUGIN_ID = "onebot-ws.goqbot-adapter"
GATEWAY_NAME = "goqbot_gateway"
GATEWAY_PROTOCOL = "onebot11-goqbot"

# 配置结构版本：结构有不兼容变化时递增（旧版本配置由 config.py 自动迁移）。
SUPPORTED_CONFIG_VERSION = "1.1.0"

DEFAULT_PLATFORM = "qq"

# 连接方式取值即配置页下拉框里显示的文字。
MODE_FORWARD = "正向"
MODE_REVERSE = "反向"
DEFAULT_ADDRESS = "ws://127.0.0.1:6700"
DEFAULT_RECONNECT_DELAY_SEC = 5.0
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30.0
DEFAULT_ACTION_TIMEOUT_SEC = 30.0

# goqbot 在 send_*_msg 带 passive_only=true 且没有可用被动凭证时返回的错误码。
RET_NO_PASSIVE = 1460
# 首个支持 passive_only 参数的 goqbot 版本。
PASSIVE_ONLY_MIN_GOQBOT_VERSION = "1.11.0"

# MaiBot 内部保留、不能作为外部平台名的标识。
RESERVED_PLATFORMS = frozenset({"webui", "internal"})
