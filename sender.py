"""出站发送策略：被动优先，按开关与单群频率限制决定是否降级为主动消息。

依赖 goqbot 的 ``passive_only`` 参数：带上它时，goqbot 在没有可用被动凭证的情况下
不会自动降级为主动消息，而是返回 ``retcode=1460``，由这里决定下一步。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Mapping, Optional

import time

from .codec import OutboundPlan
from .config import ActiveMessageConfig
from .constants import RET_NO_PASSIVE

CallAction = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


@dataclass
class SendResult:
    success: bool
    message_id: str = ""
    mode: str = ""  # passive | active | dropped
    action: str = ""
    error: str = ""
    retcode: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class GroupRateLimiter:
    """按群统计主动消息的滑动窗口限速器。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._history: Dict[int, Deque[float]] = {}

    def try_acquire(self, group_id: int, max_count: int, window_sec: float) -> bool:
        now = self._clock()
        history = self._history.setdefault(group_id, deque())
        while history and now - history[0] >= window_sec:
            history.popleft()
        if len(history) >= max_count:
            return False
        history.append(now)
        return True

    def refund(self, group_id: int) -> None:
        """主动消息实际发送失败时退还额度（QQ 只对成功发送计数）。"""

        history = self._history.get(group_id)
        if history:
            history.pop()

    def remaining_wait(self, group_id: int, max_count: int, window_sec: float) -> float:
        history = self._history.get(group_id)
        if not history or len(history) < max_count:
            return 0.0
        return max(0.0, window_sec - (self._clock() - history[0]))

    def clear(self) -> None:
        self._history.clear()


def _ok(response: Mapping[str, Any]) -> bool:
    return str(response.get("status") or "").lower() == "ok" and response.get("retcode", 0) == 0


def _retcode(response: Mapping[str, Any]) -> Optional[int]:
    value = response.get("retcode")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_text(response: Mapping[str, Any]) -> str:
    return str(response.get("wording") or response.get("message") or response.get("msg") or "发送失败")


def _message_id(response: Mapping[str, Any]) -> str:
    data = response.get("data")
    if isinstance(data, Mapping):
        return str(data.get("message_id") or "")
    return ""


class OutboundSender:
    """执行一条出站消息的发送。"""

    def __init__(self, call_action: CallAction, logger: Any, limiter: Optional[GroupRateLimiter] = None) -> None:
        self._call_action = call_action
        self._logger = logger
        self.limiter = limiter or GroupRateLimiter()

    async def send(self, plan: OutboundPlan, active: ActiveMessageConfig) -> SendResult:
        is_group = plan.chat_type == "group"
        id_key = "group_id" if is_group else "user_id"
        passive_action = "send_group_msg" if is_group else "send_private_msg"
        active_action = "send_group_active_msg" if is_group else "send_private_active_msg"
        target_desc = f"{'群' if is_group else '私聊'} {plan.target_id}"
        segments: List[Dict[str, Any]] = plan.segments

        response = await self._call_action(
            passive_action, {id_key: plan.target_id, "message": segments, "passive_only": True}
        )
        if _ok(response):
            return SendResult(True, _message_id(response), "passive", passive_action)

        code = _retcode(response)
        if code != RET_NO_PASSIVE:
            return SendResult(False, mode="passive", action=passive_action, error=_error_text(response), retcode=code)

        # —— 没有被动凭证：按开关决定是否发主动消息 ——
        if is_group:
            if not active.group_enabled:
                self._logger.info(f"[goqbot] {target_desc} 没有可用的被动回复凭证，群聊主动消息已关闭，本条消息已丢弃")
                return SendResult(False, mode="dropped", action=passive_action,
                                  error="无被动凭证且群聊主动消息已关闭", retcode=code)
            if not self.limiter.try_acquire(plan.target_id, active.group_max_per_window, active.group_window_sec):
                wait = self.limiter.remaining_wait(plan.target_id, active.group_max_per_window, active.group_window_sec)
                self._logger.info(
                    f"[goqbot] {target_desc} 主动消息超出单群频率限制"
                    f"（{active.group_max_per_window} 条/{active.group_window_sec} 秒，约 {wait:.0f} 秒后恢复），本条消息已丢弃"
                )
                return SendResult(False, mode="dropped", action=passive_action,
                                  error="超出单群主动消息频率限制", retcode=code)
        elif not active.private_enabled:
            self._logger.info(f"[goqbot] {target_desc} 没有可用的被动回复凭证，私聊主动消息已关闭，本条消息已丢弃")
            return SendResult(False, mode="dropped", action=passive_action,
                              error="无被动凭证且私聊主动消息已关闭", retcode=code)

        # 主动消息不能带引用（goqbot 会忽略 reply，这里也去掉以免误解）
        active_segments = [s for s in segments if s.get("type") != "reply"] or segments
        try:
            response = await self._call_action(active_action, {id_key: plan.target_id, "message": active_segments})
        except Exception:
            if is_group:
                self.limiter.refund(plan.target_id)
            raise
        if _ok(response):
            self._logger.info(f"[goqbot] {target_desc} 已改发主动消息")
            return SendResult(True, _message_id(response), "active", active_action)
        if is_group:
            self.limiter.refund(plan.target_id)
        return SendResult(False, mode="active", action=active_action, error=_error_text(response),
                          retcode=_retcode(response))
