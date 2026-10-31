"""goqbot（OneBot V11）报文与 MaiBot 标准消息字典之间的转换。

入站：goqbot message 事件 → MaiBot ``MessageDict``（交给 ``ctx.gateway.route_message``）。
出站：MaiBot ``MessageDict`` → 发送目标 + OneBot 消息段数组。

goqbot 的特点（与 NapCat 不同，需要在这里兜底）：
- user_id / group_id 是 goqbot 用 openid 映射出的数字，不是真实 QQ 号；
- 群消息事件不带 group_name，sender.nickname 经常为空；
- 用户 @ 机器人时，只有 goqbot 配置 ``qq.mention_mode: at`` 才会下发 at 段（qq=self_id）；
- 图片只给 url（QQ 多媒体链接），需要下载后转 base64 给 MaiBot。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import base64
import hashlib
import time

ImageLoader = Callable[[str], Awaitable[Optional[bytes]]]
GroupNameResolver = Callable[[str], Awaitable[str]]

Segment = Dict[str, Any]


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _text_seg(text: str) -> Segment:
    return {"type": "text", "data": text}


# ── 入站 ────────────────────────────────────────────────────────


@dataclass
class InboundResult:
    message: Dict[str, Any]
    external_message_id: str


class InboundCodec:
    """把 goqbot 消息事件转换为 MaiBot 消息字典。"""

    def __init__(self, load_image: ImageLoader, resolve_group_name: GroupNameResolver) -> None:
        self._load_image = load_image
        self._resolve_group_name = resolve_group_name

    async def build(
        self,
        payload: Mapping[str, Any],
        *,
        platform: str,
        account_id: str,
        download_images: bool,
    ) -> Optional[InboundResult]:
        """转换一条 message 事件；不是可投递的消息时返回 ``None``。"""

        message_type = _text(payload.get("message_type")) or "private"
        sender = payload.get("sender")
        sender = sender if isinstance(sender, Mapping) else {}
        user_id = _text(payload.get("user_id") or sender.get("user_id"))
        message_id = _text(payload.get("message_id"))
        if not user_id or not message_id:
            return None
        if account_id and user_id == account_id:
            return None  # 机器人自己的消息

        group_id = _text(payload.get("group_id")) if message_type == "group" else ""
        is_group = bool(group_id)

        segments, is_at = await self._convert_segments(
            payload.get("message"), self_id=account_id, download_images=download_images
        )
        if not segments:
            raw = _text(payload.get("raw_message"))
            segments = [_text_seg(raw or "[不支持的消息]")]

        plain_text = build_plain_text(segments)
        nickname = _text(sender.get("nickname")) or _text(sender.get("card")) or user_id
        cardname = _text(sender.get("card")) or None

        additional_config: Dict[str, Any] = {"goqbot_message_type": message_type}
        if is_group:
            additional_config["platform_io_target_group_id"] = group_id
        else:
            additional_config["platform_io_target_user_id"] = user_id
        for key in ("union_openid", "user_openid", "group_openid"):
            if value := _text(payload.get(key)):
                additional_config[f"goqbot_{key}"] = value
        if role := _text(sender.get("role")):
            additional_config["goqbot_sender_role"] = role

        message_info: Dict[str, Any] = {
            "user_info": {"user_id": user_id, "user_nickname": nickname, "user_cardname": cardname},
            "additional_config": additional_config,
        }
        if is_group:
            group_name = _text(payload.get("group_name")) or await self._resolve_group_name(group_id)
            message_info["group_info"] = {"group_id": group_id, "group_name": group_name or f"群{group_id}"}

        timestamp = payload.get("time")
        if not isinstance(timestamp, (int, float)) or timestamp <= 0:
            timestamp = time.time()

        message = {
            "message_id": message_id,
            "timestamp": str(float(timestamp)),
            "platform": platform,
            "account_id": account_id,
            "scope": None,
            "message_info": message_info,
            "raw_message": segments,
            "is_mentioned": is_at,
            "is_at": is_at,
            "is_emoji": False,
            "is_picture": any(s.get("type") == "image" for s in segments),
            "is_command": plain_text.startswith("/"),
            "is_notify": False,
            "session_id": "",
            "processed_plain_text": plain_text,
            "display_message": plain_text,
        }
        return InboundResult(message=message, external_message_id=message_id)

    async def _convert_segments(
        self, raw: Any, *, self_id: str, download_images: bool
    ) -> "tuple[List[Segment], bool]":
        if isinstance(raw, str):
            # message_format=string 时 goqbot 仍下发段数组；这里只兜底纯文本
            return ([_text_seg(raw)] if raw else []), False
        if not isinstance(raw, list):
            return [], False

        out: List[Segment] = []
        is_at = False
        for seg in raw:
            if not isinstance(seg, Mapping):
                continue
            seg_type = _text(seg.get("type"))
            data = seg.get("data")
            data = data if isinstance(data, Mapping) else {}

            if seg_type == "text":
                if text := str(data.get("text") or ""):
                    out.append(_text_seg(text))
            elif seg_type == "markdown":
                if content := str(data.get("content") or ""):
                    out.append(_text_seg(content))
            elif seg_type == "at":
                target = _text(data.get("qq"))
                if not target:
                    continue
                if target == "all":
                    out.append(_text_seg("@全体成员"))
                    continue
                name = _text(data.get("name")) or None
                out.append(
                    {
                        "type": "at",
                        "data": {
                            "target_user_id": target,
                            "target_user_nickname": name,
                            "target_user_cardname": None,
                        },
                    }
                )
                if self_id and target == self_id:
                    is_at = True
            elif seg_type == "reply":
                target = _text(data.get("id"))
                if target and target != "0":
                    out.append({"type": "reply", "data": {"target_message_id": target}})
            elif seg_type == "image":
                out.append(await self._image_segment(data, download_images))
            elif seg_type in {"record", "voice", "audio"}:
                out.append(_text_seg("[语音]"))
            elif seg_type == "video":
                out.append(_text_seg("[视频]"))
            elif seg_type == "file":
                name = _text(data.get("name")) or "附件"
                out.append(_text_seg(f"[文件:{name}]"))
            elif seg_type == "face":
                out.append(_text_seg("[表情]"))
            elif seg_type == "forward":
                out.append(_text_seg("[合并转发]"))
            # keyboard / stream / ark 等 QQ 专属段对 MaiBot 无意义，直接忽略
        return out, is_at

    async def _image_segment(self, data: Mapping[str, Any], download: bool) -> Segment:
        url = _text(data.get("url")) or _text(data.get("file"))
        if not download or not url:
            return _text_seg("[图片]")
        binary: Optional[bytes] = None
        if url.startswith("base64://"):
            try:
                binary = base64.b64decode(url[len("base64://"):])
            except ValueError:
                binary = None
        elif url.startswith(("http://", "https://")):
            binary = await self._load_image(url)
        if not binary:
            return _text_seg("[图片]")
        return {
            "type": "image",
            "data": "",
            "hash": hashlib.sha256(binary).hexdigest(),
            "binary_data_base64": base64.b64encode(binary).decode("ascii"),
        }


def build_plain_text(segments: List[Segment]) -> str:
    parts: List[str] = []
    for seg in segments:
        seg_type = seg.get("type")
        data = seg.get("data")
        if seg_type == "text":
            parts.append(str(data or ""))
        elif seg_type == "at" and isinstance(data, Mapping):
            name = data.get("target_user_cardname") or data.get("target_user_nickname") or data.get("target_user_id")
            parts.append(f"@{name}")
        elif seg_type == "reply":
            parts.append("[回复]")
        elif seg_type == "image":
            parts.append("[图片]")
    return "".join(parts).strip() or "[不支持的消息]"


# ── 出站 ────────────────────────────────────────────────────────


@dataclass
class OutboundPlan:
    """一条 MaiBot 出站消息的发送计划。"""

    chat_type: str  # "group" | "private"
    target_id: int
    segments: List[Dict[str, Any]] = field(default_factory=list)
    dropped_types: List[str] = field(default_factory=list)


def _to_int(value: str, what: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} 不是 goqbot 的数字 ID: {value!r}") from exc
    if number <= 0:
        raise ValueError(f"{what} 无效: {value!r}")
    return number


class OutboundCodec:
    """把 MaiBot 出站消息转换为 goqbot 可发送的消息段。"""

    def build(self, message: Mapping[str, Any], route: Optional[Mapping[str, Any]] = None) -> OutboundPlan:
        route = route or {}
        info = message.get("message_info")
        info = info if isinstance(info, Mapping) else {}
        group_info = info.get("group_info")
        group_info = group_info if isinstance(group_info, Mapping) else {}
        extra = info.get("additional_config")
        extra = extra if isinstance(extra, Mapping) else {}

        group_id = _text(
            group_info.get("group_id")
            or extra.get("platform_io_target_group_id")
            or route.get("group_id")
            or route.get("target_group_id")
        )
        user_id = _text(
            extra.get("platform_io_target_user_id")
            or extra.get("target_user_id")
            or route.get("user_id")
            or route.get("target_user_id")
        )
        if group_id:
            plan = OutboundPlan("group", _to_int(group_id, "group_id"))
        elif user_id:
            plan = OutboundPlan("private", _to_int(user_id, "user_id"))
        else:
            raise ValueError("出站消息缺少目标（既没有 group_id 也没有私聊 user_id）")

        raw = message.get("raw_message")
        raw_list = raw if isinstance(raw, list) else [raw] if raw else []
        self._convert(raw_list, plan)
        if not any(s["type"] != "reply" for s in plan.segments):
            raise ValueError("出站消息不包含可发送的内容")
        # goqbot 只认第一个 reply 段；多余的引用去掉
        seen_reply = False
        cleaned: List[Dict[str, Any]] = []
        for seg in plan.segments:
            if seg["type"] == "reply":
                if seen_reply:
                    continue
                seen_reply = True
            cleaned.append(seg)
        plan.segments = cleaned
        return plan

    def _convert(self, items: List[Any], plan: OutboundPlan) -> None:
        for item in items:
            if isinstance(item, str):
                plan.segments.append({"type": "text", "data": {"text": item}})
                continue
            if not isinstance(item, Mapping):
                continue
            seg_type = _text(item.get("type"))
            data = item.get("data")

            if seg_type == "text":
                if text := str(data or ""):
                    plan.segments.append({"type": "text", "data": {"text": text}})
            elif seg_type == "at":
                target = _text(data.get("target_user_id")) if isinstance(data, Mapping) else _text(data)
                if target:
                    plan.segments.append({"type": "at", "data": {"qq": target}})
            elif seg_type == "reply":
                target = _text(data.get("target_message_id")) if isinstance(data, Mapping) else _text(data)
                if target.isdigit() and target != "0":
                    plan.segments.append({"type": "reply", "data": {"id": target}})
            elif seg_type in {"image", "emoji"}:
                b64 = _text(item.get("binary_data_base64"))
                if b64:
                    plan.segments.append({"type": "image", "data": {"file": f"base64://{b64}"}})
                else:
                    plan.dropped_types.append(seg_type)
            elif seg_type == "imageurl":
                url = _text(data.get("url") if isinstance(data, Mapping) else data)
                if url:
                    plan.segments.append({"type": "image", "data": {"file": url}})
            elif seg_type == "voice":
                b64 = _text(item.get("binary_data_base64"))
                if b64:
                    plan.segments.append({"type": "record", "data": {"file": f"base64://{b64}"}})
                else:
                    plan.dropped_types.append(seg_type)
            elif seg_type == "voiceurl":
                url = _text(data.get("url") if isinstance(data, Mapping) else data)
                if url:
                    plan.segments.append({"type": "record", "data": {"file": url}})
            elif seg_type == "forward" and isinstance(data, list):
                # QQ 官方机器人不支持合并转发：把每个节点的内容平铺发送
                for node in data:
                    if isinstance(node, Mapping):
                        content = node.get("content") or node.get("message") or node.get("raw_message")
                        if isinstance(content, list):
                            self._convert(content, plan)
                        elif isinstance(content, str) and content:
                            plan.segments.append({"type": "text", "data": {"text": content}})
            elif seg_type == "seglist" and isinstance(data, list):
                self._convert(data, plan)
            else:
                plan.dropped_types.append(seg_type or "unknown")
