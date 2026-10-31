from __future__ import annotations

import asyncio
import logging

from conftest import load

codec = load("codec")
config = load("config")
sender_mod = load("sender")

LOGGER = logging.getLogger("test.goqbot")


class FakeGoqbot:
    def __init__(self, passive_available=False, active_ok=True):
        self.passive_available = passive_available
        self.active_ok = active_ok
        self.calls = []

    async def __call__(self, action, params):
        self.calls.append((action, params))
        if action in ("send_group_msg", "send_private_msg"):
            if self.passive_available:
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
            return {"status": "failed", "retcode": 1460, "data": None, "message": "无可用被动凭证"}
        if action.endswith("_active_msg"):
            if self.active_ok:
                return {"status": "ok", "retcode": 0, "data": {"message_id": 2}}
            return {"status": "failed", "retcode": 1500, "message": "主动消息额度不足"}
        raise AssertionError(action)


def plan(chat="group"):
    seg = [{"type": "reply", "data": {"id": "9"}}, {"type": "text", "data": {"text": "hi"}}]
    return codec.OutboundPlan(chat, 2002, list(seg))


def active(**kw):
    return config.ActiveMessageConfig(**kw)


def run(goq, p, a, limiter=None):
    s = sender_mod.OutboundSender(goq, LOGGER, limiter)
    return asyncio.run(s.send(p, a))


def test_passive_success_uses_passive_only():
    goq = FakeGoqbot(passive_available=True)
    result = run(goq, plan(), active())
    assert result.success and result.mode == "passive" and result.message_id == "1"
    assert goq.calls == [("send_group_msg", {"group_id": 2002, "message": plan().segments, "passive_only": True})]


def test_group_active_disabled_drops():
    goq = FakeGoqbot()
    result = run(goq, plan(), active())
    assert not result.success and result.mode == "dropped" and "已关闭" in result.error
    assert [c[0] for c in goq.calls] == ["send_group_msg"]


def test_group_active_enabled_sends_without_reply():
    goq = FakeGoqbot()
    result = run(goq, plan(), active(group_enabled=True))
    assert result.success and result.mode == "active" and result.message_id == "2"
    assert goq.calls[1] == ("send_group_active_msg", {"group_id": 2002, "message": [{"type": "text", "data": {"text": "hi"}}]})


def test_group_rate_limit_and_refund():
    clock = [0.0]
    limiter = sender_mod.GroupRateLimiter(clock=lambda: clock[0])
    a = active(group_enabled=True, group_max_per_window=2, group_window_sec=60)
    goq = FakeGoqbot()
    assert run(goq, plan(), a, limiter).success
    assert run(goq, plan(), a, limiter).success
    third = run(goq, plan(), a, limiter)
    assert not third.success and "频率限制" in third.error
    clock[0] = 61
    assert run(goq, plan(), a, limiter).success
    # 发送失败时退还额度
    failing = FakeGoqbot(active_ok=False)
    limiter2 = sender_mod.GroupRateLimiter(clock=lambda: 0.0)
    a1 = active(group_enabled=True, group_max_per_window=1, group_window_sec=60)
    assert not run(failing, plan(), a1, limiter2).success
    assert run(FakeGoqbot(), plan(), a1, limiter2).success


def test_private_switch_is_independent():
    goq = FakeGoqbot()
    assert not run(goq, plan("private"), active(group_enabled=True)).success
    goq = FakeGoqbot()
    result = run(goq, plan("private"), active(private_enabled=True))
    assert result.success and goq.calls[-1][0] == "send_private_active_msg"
    assert goq.calls[0][1]["user_id"] == 2002 and goq.calls[0][1]["passive_only"] is True


def test_other_errors_are_not_retried_as_active():
    async def goq(action, params):
        return {"status": "failed", "retcode": 1404, "message": "QQ 发送通道未就绪"}

    result = run(goq, plan(), active(group_enabled=True))
    assert not result.success and result.retcode == 1404 and result.mode == "passive"
