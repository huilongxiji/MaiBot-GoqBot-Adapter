# MaiBot GoqBot Adapter

把 [MaiBot](https://github.com/Mai-with-u/MaiBot) 接到 **goqbot**（QQ 官方机器人 ↔ OneBot V11 协议端）的适配器插件。

goqbot 和 go-cqhttp 一样，可以同时把事件广播给多个 OneBot 下游。装上本插件后，
同一个 QQ 官方机器人可以**同时**接入 NoneBot 和 MaiBot：所有消息两边都会收到，各自的框架决定要不要回复，插件不做任何回复协调。

需要 MaiBot >= 1.3.18，goqbot >= 1.11.0（`passive_only` 支持，用于主动消息开关）。

## 安装

把本仓库放到 MaiBot 的 `plugins/` 目录下（目录名任意），重启 MaiBot 后在 WebUI 的插件页启用并填写配置。

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

插件配置：连接方式 `forward`，正向地址 `ws://127.0.0.1:6700`，访问令牌与 goqbot 一致。

### 反向：goqbot 连插件

插件配置：连接方式 `reverse`，监听 `127.0.0.1:8095`，路径 `/onebot/v11/ws`。goqbot 中新增一条 ws-reverse：

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

## 连接标识

默认 `qq`。同一个 MaiBot 同时接入多个协议端（例如两个 goqbot，或者 goqbot + NapCat）时，
给每个协议端填不同的标识（如 `qq_official`），避免不同来源的用户 / 群号混在一起。
goqbot 的 user_id / group_id 是由 openid 映射出的数字，不是真实 QQ 号，与 NapCat 共用 `qq` 时尤其建议区分。

## 主动消息

QQ 官方机器人的回复分为**被动回复**（针对用户刚发的消息，群 5 分钟内 5 次、私聊 60 分钟内 4 次，
额度由 goqbot 的所有下游共用）和**主动消息**（官方频控严格：机器人维度 60 条/分钟，单群每天 1000 条，群主需开启「机器人在群聊内发言」）。

插件总是先以 `passive_only=true` 尝试被动回复。没有可用被动凭证时：

| 场景 | 开关关闭（默认） | 开关开启 |
| --- | --- | --- |
| 群聊 | 丢弃并记录日志 | 在「单群频率限制」内改发主动消息，超出则丢弃 |
| 私聊 | 丢弃并记录日志 | 改发主动消息（独立开关） |

## 建议的 goqbot 设置

- `qq.mention_mode: at`：用户 @ 机器人时 goqbot 会下发 `at` 段（qq=self_id），MaiBot 才能识别「被 @」。
  默认的 `strip` 会删掉这段 @，MaiBot 只能把它当普通消息。NoneBot 的 OneBot 适配器同样能识别 at 自身，一般不受影响。
- 群聊想让 MaiBot 看到全部消息，需要在 `text_intent` 中开启 `GroupMessageEventHandler`，并由群主开启「获取群内全部消息」。

## 支持范围

| 方向 | 支持 | 说明 |
| --- | --- | --- |
| 入站 | 文本、@、引用、图片 | 语音 / 视频 / 文件 / 表情 / 合并转发转为占位文本；notice 事件暂不转发 |
| 出站 | 文本、@、引用、图片、语音 | 合并转发平铺为普通消息发送 |

群名：goqbot 群事件不带群名，插件会尝试一次 `get_group_info`（白名单接口），失败则使用 `群<group_id>`。

## 开发

```bash
# 在 MaiBot 的虚拟环境中
python -m pytest tests
```
