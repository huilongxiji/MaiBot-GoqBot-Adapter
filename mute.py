"""禁言指令：只认「允许触发的用户」亲自发出的、格式固定的指令。

禁言的对象、动作和时长全部从 goqbot 下发的原始群消息中解析，模型只能指出是哪一条指令：

- 指令必须以「禁言」「解禁」或「解除禁言」开头（可带前导 ``/``，前面 @机器人 不影响），
  例如 ``禁言 @小明 10分钟``、``/解禁 @小明``；
- 指令中必须 @ 且只 @ 一位成员（@机器人 不计入），被 @ 的人就是目标；
- 时长写在指令里（秒 / 分钟 / 小时 / 天，纯数字按分钟），不写默认 10 分钟；
- 每条指令只能执行一次。

指令缓存只保存插件本次运行中亲自收到的群消息，模型无法伪造，也无法引用插件重启前的旧消息。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import re
import time

MUTE_KEYWORDS: Tuple[Tuple[str, str], ...] = (("解除禁言", "unmute"), ("解禁", "unmute"), ("禁言", "mute"))
DEFAULT_MUTE_SEC = 10 * 60
COMMAND_MAX_AGE_SEC = 10 * 60
_CACHE_LIMIT = 2000

_UNIT_SECONDS = {
    "秒": 1, "s": 1, "sec": 1,
    "分钟": 60, "分": 60, "m": 60, "min": 60,
    "小时": 3600, "时": 3600, "h": 3600,
    "天": 86400, "d": 86400,
}
_DURATION_RE = re.compile(r"^\s*(\d{1,7})\s*(秒|分钟|分|小时|时|天|sec|min|s|m|h|d)?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class MuteCommand:
    action: str  # mute | unmute
    target_id: str
    target_name: str
    duration: int  # 解禁时为 0


@dataclass
class CachedGroupMessage:
    message_id: str
    group_id: str
    user_id: str
    segments: List[Dict[str, Any]]
    received_at: float
    consumed: bool = field(default=False)


def parse_duration(text: str) -> Tuple[Optional[int], str]:
    """解析时长文本；空文本返回默认值。返回 (秒数, 错误信息)。"""

    stripped = text.strip()
    if not stripped:
        return DEFAULT_MUTE_SEC, ""
    match = _DURATION_RE.match(stripped)
    if not match:
        return None, f"无法识别禁言时长「{stripped}」，请写成如 30秒 / 10分钟 / 2小时 / 1天"
    unit = (match.group(2) or "分钟").lower()
    seconds = int(match.group(1)) * _UNIT_SECONDS[unit]
    if seconds <= 0:
        return None, "禁言时长必须大于 0（解除禁言请用「解禁 @某人」）"
    return seconds, ""


def parse_mute_command(segments: Any, self_id: str) -> Tuple[Optional[MuteCommand], str]:
    """从原始 OneBot 消息段解析禁言指令。返回 (指令, 不是合法指令时的原因)。"""

    if not isinstance(segments, list):
        return None, "消息格式无法识别"
    texts: List[str] = []
    targets: List[Tuple[str, str]] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), Mapping) else {}
        if kind == "text":
            texts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "").strip()
            if qq and qq != str(self_id) and qq != "all":
                targets.append((qq, str(data.get("name") or "").strip()))
        elif kind == "reply":
            continue
        else:
            return None, "指令中只能包含文字和 @"
    text = "".join(texts).strip().lstrip("/／").strip()
    for keyword, action in MUTE_KEYWORDS:
        if text.startswith(keyword):
            rest = text[len(keyword):]
            break
    else:
        return None, "不是禁言指令（指令须以「禁言」「解禁」或「解除禁言」开头）"
    if len(targets) != 1:
        return None, "禁言指令中必须 @ 且只能 @ 一位成员"
    target_id, target_name = targets[0]
    if action == "unmute":
        if rest.strip():
            return None, "解禁指令中不能带其他内容"
        return MuteCommand("unmute", target_id, target_name, 0), ""
    seconds, error = parse_duration(rest)
    if seconds is None:
        return None, error
    return MuteCommand("mute", target_id, target_name, seconds), ""


class RecentGroupMessages:
    """插件亲自收到的近期群消息（按 message_id 索引）。"""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._items: "OrderedDict[str, CachedGroupMessage]" = OrderedDict()

    def add(self, message_id: str, group_id: str, user_id: str, segments: Any) -> None:
        if not message_id or not isinstance(segments, list):
            return
        self._prune()
        self._items[message_id] = CachedGroupMessage(
            message_id, group_id, user_id, [dict(s) for s in segments if isinstance(s, Mapping)], self._clock()
        )
        self._items.move_to_end(message_id)
        while len(self._items) > _CACHE_LIMIT:
            self._items.popitem(last=False)

    def get(self, message_id: str) -> Optional[CachedGroupMessage]:
        self._prune()
        return self._items.get(message_id)

    def age(self, item: CachedGroupMessage) -> float:
        return self._clock() - item.received_at

    def clear(self) -> None:
        self._items.clear()

    def _prune(self) -> None:
        now = self._clock()
        while self._items:
            first = next(iter(self._items.values()))
            if now - first.received_at <= COMMAND_MAX_AGE_SEC:
                break
            self._items.popitem(last=False)
