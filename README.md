# MaiBot GoqBot Adapter

把 [MaiBot](https://github.com/Mai-with-u/MaiBot) 接到 **goqbot**（QQ 官方机器人 ↔ OneBot V11 协议端）的适配器插件。

goqbot 和 go-cqhttp 一样，可以同时把事件广播给多个 OneBot 下游。装上本插件后，
同一个 QQ 官方机器人可以**同时**接入 NoneBot 和 MaiBot：所有消息两边都会收到，各自的框架决定要不要回复，插件不做任何回复协调。

需要 MaiBot >= 1.3.18，goqbot >= 1.11.0（`passive_only` 支持，用于主动消息开关）。

## 安装

把本仓库放到 MaiBot 的 `plugins/` 目录下（目录名任意），重启 MaiBot 后在 WebUI 的插件页启用并填写配置。

## 配置页

| 分组 | 内容（从上到下） |
| --- | --- |
| 插件设置 | 启用适配器、连接标识 |
| 连接 | 连接方式（正向 / 反向）、地址、访问令牌、重连等待、心跳间隔、动作超时 |
| 主动消息 | 私聊主动消息、群聊主动消息、单群最多条数、时间窗口 |
| 消息 | 图片下载 |
| 聊天工具 | 6 个 goqbot 特有工具的开关、禁止聊天触发召回（见下文「聊天工具」） |
| 禁言工具 | 允许触发的用户、禁言保护名单、允许禁言的群、最长禁言时长（见下文「禁言工具的安全限制」） |
| 开放接口 | 6 组供其他插件调用的接口开关（见下文「开放接口」） |
| 调试 | 原始报文日志 |

黑白名单不在插件配置页里，见下文「黑白名单」。

配置页顶部会显示 goqbot 连接状态（连上后显示为「QQ 机器人 ID，goqbot 程序版本 x.x.x」），并提供「重新连接」按钮。

## 地址格式

配置页「地址」一栏按连接方式填写完整的 WebSocket 地址：

| 连接方式 | 地址填什么 | 示例 |
| --- | --- | --- |
| 正向 | goqbot 正向 ws 服务的地址（`ws://` 或 `wss://`，端口必填或使用默认 80/443） | `ws://127.0.0.1:6700` |
| 反向 | 插件自己要监听的地址，必须是 `ws://`，端口必填，路径可省略（默认 `/`） | `ws://127.0.0.1:8095/onebot/v11/ws` |

- 反向时 goqbot 的 `ws-reverse.universal` 要填**同一个地址**。
- 反向监听 `0.0.0.0`（允许其他机器 / 容器连入）时，goqbot 那边填插件所在机器的实际 IP，例如 `ws://192.168.1.10:8095/onebot/v11/ws`，并务必设置访问令牌。
- 反向不支持 `wss://`，需要 TLS 时请在前面加一层反向代理。
- 地址格式不正确时插件不会建立连接，原因会显示在配置页顶部的连接状态里。

## 连接方式（二选一）

### 正向：插件连 goqbot

goqbot 的 `config.yml` 中保留一条正向 ws（NoneBot 等也可以继续连同一个端口）：

```yaml
onebot:
  access_token: "your-token"
  servers:
    - ws:
        address: 0.0.0.0:6700
```

插件配置：连接方式选「正向」，地址填 `ws://127.0.0.1:6700`，访问令牌与 goqbot 一致。

### 反向：goqbot 连插件

插件配置：连接方式选「反向」，地址填插件要监听的完整地址，例如 `ws://127.0.0.1:8095/onebot/v11/ws`。
goqbot 中新增一条 ws-reverse，`universal` 填同一个地址（插件监听 `0.0.0.0` 时，这里填插件所在机器的实际 IP）：

```yaml
onebot:
  access_token: "your-token"
  servers:
    - ws-reverse:                       # 已有的 NoneBot
        universal: ws://127.0.0.1:8080/onebot/v11/ws
    - ws-reverse:                       # MaiBot（本插件）
        universal: ws://127.0.0.1:8095/onebot/v11/ws
        reconnect-interval: 3000
```

> goqbot 的 `access_token` 对所有下游生效，插件里填写同一个值即可。
> 「重连等待」只对正向生效；反向的重连间隔由 goqbot 的 `reconnect-interval` 决定。

## 连接标识

位于「插件设置」分组，默认 `qq`。同一个 MaiBot 同时接入多个协议端（例如两个 goqbot，或者 goqbot + NapCat）时，
给每个协议端填不同的标识（如 `qq_official`），避免不同来源的用户 / 群号混在一起。
goqbot 的 user_id / group_id 是由 openid 映射出的数字，不是真实 QQ 号，与 NapCat 共用 `qq` 时尤其建议区分。

## 主动消息

QQ 官方机器人的回复分为**被动回复**（针对用户刚发的消息，群 5 分钟内 5 次、私聊 60 分钟内 4 次，
额度由 goqbot 的所有下游共用）和**主动消息**（官方频控严格：机器人维度 60 条/分钟，单群每天 1000 条，群主需开启「机器人在群聊内发言」）。

插件总是先以 `passive_only=true` 尝试被动回复。没有可用被动凭证时：

| 场景 | 开关关闭（默认） | 开关开启 |
| --- | --- | --- |
| 私聊 | 丢弃并记录日志 | 改发主动消息（独立开关） |
| 群聊 | 丢弃并记录日志 | 在「单群频率限制」内改发主动消息，超出则丢弃 |

## 黑白名单

本插件不自带黑白名单，使用 MaiBot WebUI「适配器设置 → 黑白名单规则」中为本适配器自动生成的规则面板。

**名单只能填写 goqbot 下发的数字 ID，不能填写 openid：**

| 规则 | 填写内容 |
| --- | --- |
| 群聊规则 | `group_id`（goqbot 由 group_openid 映射出的数字） |
| 私聊规则 | `user_id`（goqbot 由 user_openid 映射出的数字） |

- `user_openid`、`group_openid`、`union_openid` 填进去不会生效。
- 这些数字 ID 就是 MaiBot 聊天记录 / 用户信息里显示的 ID，也可以在「调试 → 打印原始报文」开启后从日志里查到。
- QQ 官方机器人里同一个人在私聊和群聊中的 ID 不同；私聊规则只对私聊生效，群里单个发言人不在框架名单的过滤范围内。

## 聊天工具

让 MaiBot 的模型在聊天中直接使用 goqbot 特有的能力。每个工具单独开关，**默认全部关闭**，保存配置后立即生效；
开启的工具会出现在 WebUI 插件详情的「已注册组件」中，且只在本连接（连接标识）的会话里可用。

| 开关 | 工具名 | 作用 | 说明 |
| --- | --- | --- | --- |
| Markdown 消息 | `goqbot_send_markdown` | 发送原生 Markdown，可附带按钮 | 按钮支持跳转（link）和指令（command），最多 15 个；遵循「主动消息」开关 |
| 图文卡片 | `goqbot_send_card` | 发送标题 + 简介 + 封面图的链接卡片 | 链接与图片必须是 https；遵循「主动消息」开关 |
| 互动召回 | `goqbot_send_wakeup` | 给当前私聊的用户发互动召回消息 | 仅私聊，只能召回当前聊天的用户；默认禁止聊天触发（见下文）；30 天内分 4 个周期，每周期 1 条；不受「主动消息」开关影响 |
| 主动消息权限查询 | `goqbot_get_push_state` | 查询当前群 / 私聊能否主动发消息 | 群：群主是否开启「机器人主动发言」、机器人身份；私聊：用户是否允许推送 |
| 撤回消息 | `goqbot_recall_message` | 撤回机器人自己发过的消息 | QQ 官方只能撤回机器人自己的消息，且有时间限制 |
| 群成员禁言 | `goqbot_mute_member` | 执行白名单用户发出的「禁言 @某人 时长」/「解禁 @某人」指令 | 仅群聊；需要机器人是群管理员；模型不能自行禁言，见下文「禁言工具的安全限制」 |

> 修改「连接标识」后需要重载插件（或重启 MaiBot），工具的平台范围才会随之更新。

### 互动召回：禁止聊天触发

互动召回用于用户很久没来聊天时由 AI 主动问候，不应由用户在聊天里「要求」机器人发送。
「禁止聊天触发召回」默认开启：调用时插件会查看当前私聊最近 60 分钟的记录，用户在这期间发过消息就拒绝召回
（这段时间内普通回复本来就可用）；查询聊天记录失败时同样拒绝。召回目标固定为当前私聊的用户，不能指定其他人。

### 禁言工具的安全限制

禁言只执行「允许触发的用户」**本人发出的固定格式指令**，禁言谁、禁言多久都由插件从指令原文解析，模型只能指出是哪一条指令，无法自行选择对象：

| 指令 | 说明 |
| --- | --- |
| `禁言 @某人 时长` | 时长如 `30秒` / `10分钟` / `2小时` / `1天`；纯数字按分钟；不写默认 10 分钟；超过「最长禁言时长」按上限 |
| `解禁 @某人` / `解除禁言 @某人` | 解除禁言 |

指令可以带前导 `/`，前面 @机器人 不影响；指令里必须 @ 且只能 @ 一位成员（需要 goqbot 设置 `qq.mention_mode: at`）。

插件按以下规则逐项核对，任一不满足都不执行：

1. 当前群在「允许禁言的群」中（留空表示所有群都可用）；
2. 指令消息是插件本次运行中亲自从 goqbot 收到的本群消息，且在 10 分钟内；模型无法伪造，插件重启前的消息也不会被接受；
3. 发出指令的人在「允许触发的用户」名单中；**名单留空时任何人都无法触发禁言**；
4. 指令以「禁言」「解禁」或「解除禁言」开头，格式完整（「别禁言 @某人」「他刷屏了禁言他吧」这类话都不算指令）；
5. 每条指令只能执行一次；
6. 被 @ 的人不是机器人自己，也不在「禁言保护名单」中（解除禁言不受保护名单限制）。

名单都填 goqbot 下发的数字 ID（`user_id` / `group_id`），同一个人在私聊和不同群中的 `user_id` 可能不同，请按该群聊天记录中显示的 ID 填写。
每次禁言 / 解禁都会在 MaiBot 日志中记录指令发出者和目标。

## 开放接口

供其他 MaiBot 插件通过 `ctx.api.call("adapter.goqbot.xxx", ...)` 调用。每组单独开关，**默认全部关闭**，关闭的接口会立即下线。
参数与 goqbot 同名动作一致（见 [goqbot 文档](https://huilongxiji.github.io/gd-goqbot-docs/)），返回统一为
`{"success": bool, "retcode": int, "data": ..., "message": str}`。

| 开关 | API | 对应 goqbot 动作 |
| --- | --- | --- |
| 消息发送 | `message.send` | `message_type`（group / private）、`target_id`、`message`（段数组或文本）、`mode`：`auto`（默认，按「主动消息」开关处理）/ `passive` / `active` / `wakeup`（仅私聊） |
| 素材库 | `media.upload_image` / `media.get_image` / `media.list_images` / `media.delete_image` | `upload_media_image` / `get_media_image` / `get_media_images` / `delete_media_image` |
| 菜单与指令面板 | `panel.get_menu` / `panel.set_menu` / `panel.list` / `panel.get` / `panel.create` / `panel.update` / `panel.delete` / `panel.set_target` | `get_menu` / `set_menu` / `get_command_panels` / `get_command_panel` / `create_command_panel` / `set_command_panel` / `delete_command_panel` / `set_command_panel_target` |
| 状态查询 | `query.c2c_msg_state` / `query.group_bot_state` / `query.openid` / `query.group_ban` / `query.group_member` / `query.group_members` / `query.group_join_requests` | `get_c2c_msg_state` / `get_group_bot_state` / `get_openid` / `get_group_ban` / `get_group_member_info` / `get_group_member_list` / `get_group_join_requests` |
| 群管理 | `group.set_ban` / `group.handle_join_request` | `set_group_ban` / `set_group_add_request` |
| 通用动作透传 | `raw.call_action` | 任意动作：`action`、`params`（字典）。权限最大，只在信任所有已安装插件时开启 |

表中 API 名省略了前缀 `adapter.goqbot.`，例如素材上传的完整名称是 `adapter.goqbot.media.upload_image`：

```python
result = await self.ctx.api.call(
    "adapter.goqbot.message.send",
    message_type="group",
    target_id=123456,
    message=[{"type": "markdown", "data": {"data": {"markdown": {"content": "**你好**"}}}}],
)
```

## 建议的 goqbot 设置

- `qq.mention_mode: at`：用户 @ 机器人时 goqbot 会下发 `at` 段（qq=self_id），MaiBot 才能识别「被 @」。
  默认的 `strip` 会删掉这段 @，MaiBot 只能把它当普通消息。NoneBot 的 OneBot 适配器同样能识别 at 自身，一般不受影响。
- 群聊想让 MaiBot 看到全部消息，需要在 `text_intent` 中开启 `GroupMessageEventHandler`，并由群主开启「获取群内全部消息」。

## 支持范围

| 方向 | 支持 | 说明 |
| --- | --- | --- |
| 入站 | 文本、@、引用、图片 | 语音 / 视频 / 文件 / 表情 / 合并转发转为占位文本；notice 事件暂不转发 |
| 出站 | 文本、@、引用、图片、语音 | 合并转发平铺为普通消息发送；Markdown、按钮、图文卡片等通过「聊天工具」或「开放接口」发送 |

群名：goqbot 群事件不带群名，插件会尝试一次 `get_group_info`（白名单接口），失败则使用 `群<group_id>`。

## 开发

```bash
# 在 MaiBot 的虚拟环境中
python -m pytest tests
```
