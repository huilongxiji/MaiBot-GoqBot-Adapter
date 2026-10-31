"""端到端：真实 SDK PluginContext + 假 goqbot（正向服务端 / 反向客户端）。"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio
import json
import socket

import aiohttp
from aiohttp import web
from maibot_sdk.context import PluginContext

from conftest import load

plugin_mod = load("plugin")
constants = load("constants")

TOKEN = "s3cret"
SELF_ID = 10001


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHost:
    """记录插件发往 MaiBot Host 的 RPC。"""

    def __init__(self) -> None:
        self.states: List[Dict[str, Any]] = []
        self.routed: List[Dict[str, Any]] = []

    async def rpc(self, method: str, plugin_id: str, payload: Dict[str, Any], **_: Any) -> Any:
        if method == "host.update_message_gateway_state":
            self.states.append(payload)
            return {"accepted": True}
        if method == "host.route_message":
            self.routed.append(payload)
            return {"accepted": True}
        if method == "cap.call":  # 聊天工具 / 开放接口开关同步
            return {"success": True}
        raise AssertionError(method)


class FakeGoqbotLogic:
    """模拟 goqbot 的动作处理。"""

    def __init__(self, version: str = "1.11.0") -> None:
        self.version = version
        self.calls: List[Dict[str, Any]] = []
        self.passive_available = True

    def handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(req)
        action, params, echo = req["action"], req.get("params") or {}, req.get("echo")

        def ok(data):
            return {"status": "ok", "retcode": 0, "data": data, "echo": echo}

        if action == "get_version_info":
            return ok({"app_name": "goqbot", "app_version": self.version})
        if action == "get_login_info":
            return ok({"user_id": SELF_ID, "nickname": "麦麦"})
        if action == "get_group_info":
            return {"status": "failed", "retcode": 11253, "data": None, "message": "白名单接口", "echo": echo}
        if action in ("send_group_msg", "send_private_msg"):
            if params.get("passive_only") and not self.passive_available:
                return {"status": "failed", "retcode": 1460, "data": None, "message": "无可用被动凭证", "echo": echo}
            return ok({"message_id": 777})
        if action.endswith("_active_msg"):
            return ok({"message_id": 778})
        return {"status": "failed", "retcode": 1404, "data": None, "echo": echo}


GROUP_EVENT = {
    "post_type": "message", "message_type": "group", "sub_type": "normal", "time": 1761900000,
    "self_id": SELF_ID, "group_id": 2002, "user_id": 3003, "message_id": 4004,
    "message": [{"type": "at", "data": {"qq": str(SELF_ID)}}, {"type": "text", "data": {"text": "你好"}}],
    "raw_message": "[CQ:at,qq=10001]你好", "sender": {"user_id": 3003, "nickname": "", "role": "member"},
}


def make_plugin(host: FakeHost, connection: Dict[str, Any], active: Dict[str, Any] | None = None,
                platform: str = "qq"):
    plugin = plugin_mod.GoqbotAdapterPlugin()
    plugin._set_context(PluginContext(constants.PLUGIN_ID, rpc_call=host.rpc))
    cfg = plugin.get_default_config()
    cfg["plugin"]["enabled"] = True
    cfg["plugin"]["platform"] = platform
    cfg["connection"].update({"access_token": TOKEN, "reconnect_delay_sec": 1, "action_timeout_sec": 3,
                              **connection})
    if active:
        cfg["active"].update(active)
    plugin.set_plugin_config(cfg)
    return plugin


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.02)


async def exercise(plugin, host: FakeHost, logic: FakeGoqbotLogic, push_event, platform: str):
    # 1) 网关就绪，平台名与 self_id 正确
    await wait_for(lambda: any(s["ready"] for s in host.states))
    ready = [s for s in host.states if s["ready"]][-1]
    assert ready["gateway_name"] == constants.GATEWAY_NAME
    assert ready["platform"] == platform and ready["account_id"] == str(SELF_ID)
    state = await plugin.get_config_widget_state()
    assert state["connection"].tone == "success"
    assert state["connection"].text == f"QQ {SELF_ID}，goqbot 程序版本 {logic.version}"

    # 2) 入站消息被注入，@ 机器人被识别
    await push_event(GROUP_EVENT)
    await wait_for(lambda: host.routed)
    routed = host.routed[0]
    assert routed["gateway_name"] == constants.GATEWAY_NAME and routed["dedupe_key"] == "4004"
    msg = routed["message"]
    assert msg["platform"] == platform and msg["account_id"] == str(SELF_ID) and msg["is_at"] is True
    assert msg["message_info"]["group_info"] == {"group_id": "2002", "group_name": "群2002"}

    # 3) 出站：被动成功
    outbound = {"message_id": "internal-1", "message_info": {"group_info": {"group_id": "2002"}},
                "raw_message": [{"type": "reply", "data": {"target_message_id": "4004"}},
                                {"type": "text", "data": "收到"}]}
    result = await plugin.handle_goqbot_gateway(outbound, route={})
    assert result["success"] and result["external_message_id"] == "777"
    assert result["metadata"]["mode"] == "passive"
    assert result["metadata"]["adapter_callbacks"][0]["payload"]["content"]["actual_id"] == "777"
    sent = [c for c in logic.calls if c["action"] == "send_group_msg"][-1]["params"]
    assert sent["passive_only"] is True and sent["group_id"] == 2002
    assert sent["message"][0] == {"type": "reply", "data": {"id": "4004"}}

    # 4) 出站：无被动凭证 + 主动关闭 → 丢弃
    logic.passive_available = False
    result = await plugin.handle_goqbot_gateway(outbound, route={})
    assert not result["success"] and result["metadata"]["mode"] == "dropped"
    assert not any(c["action"] == "send_group_active_msg" for c in logic.calls)


def test_forward_mode_end_to_end():
    async def scenario():
        logic = FakeGoqbotLogic()
        sockets: List[web.WebSocketResponse] = []
        auth_seen: List[str] = []

        async def ws_handler(request: web.Request):
            auth_seen.append(request.headers.get("Authorization", ""))
            if request.query.get("access_token") != TOKEN:
                return web.Response(status=401)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str(json.dumps(logic.handle(json.loads(m.data))))
            return ws

        app = web.Application()
        app.router.add_get("/", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()

        host = FakeHost()
        plugin = make_plugin(host, {"mode": "正向", "address": f"ws://127.0.0.1:{port}"})
        await plugin.on_load()
        try:
            async def push(ev):
                await wait_for(lambda: sockets)
                await sockets[-1].send_str(json.dumps(ev))

            await exercise(plugin, host, logic, push, "qq")
            assert auth_seen[0] == f"Bearer {TOKEN}"

            # 断开 → 网关下线 → 自动重连后再次就绪
            ready_count = sum(1 for s in host.states if s["ready"])
            await sockets[-1].close()
            await wait_for(lambda: host.states[-1]["ready"] is False)
            await wait_for(lambda: sum(1 for s in host.states if s["ready"]) > ready_count)
        finally:
            await plugin.on_unload()
            await runner.cleanup()
        assert host.states[-1]["ready"] is False

    asyncio.run(scenario())


def test_reverse_mode_end_to_end_with_custom_platform_and_active():
    async def scenario():
        logic = FakeGoqbotLogic()
        host = FakeHost()
        port = free_port()
        plugin = make_plugin(
            host,
            {"mode": "反向", "address": f"ws://127.0.0.1:{port}/onebot/v11/ws"},
            platform="qq_official",
        )
        await plugin.on_load()
        session = aiohttp.ClientSession()
        try:
            # 令牌错误被拒绝
            try:
                await session.ws_connect(f"ws://127.0.0.1:{port}/onebot/v11/ws",
                                         headers={"Authorization": "Token wrong", "X-Self-ID": str(SELF_ID)})
                raise AssertionError("应当被拒绝")
            except aiohttp.WSServerHandshakeError as exc:
                assert exc.status == 401

            # goqbot 风格：Authorization: Token xxx + X-Self-ID + X-Client-Role
            ws = await session.ws_connect(
                f"ws://127.0.0.1:{port}/onebot/v11/ws",
                headers={"Authorization": f"Token {TOKEN}", "X-Self-ID": str(SELF_ID),
                         "X-Client-Role": "Universal"},
            )

            async def serve():
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await ws.send_str(json.dumps(logic.handle(json.loads(m.data))))

            serve_task = asyncio.create_task(serve())

            async def push(ev):
                await ws.send_str(json.dumps(ev))

            await exercise(plugin, host, logic, push, "qq_official")

            # 打开群聊主动开关（限 1 条/窗口）→ 第一条改发主动，第二条被限速
            cfg = plugin.get_plugin_config_data()
            cfg["active"].update({"group_enabled": True, "group_max_per_window": 1})
            plugin.set_plugin_config(cfg)
            outbound = {"message_info": {"group_info": {"group_id": "2002"}},
                        "raw_message": [{"type": "text", "data": "主动"}]}
            first = await plugin.handle_goqbot_gateway(outbound)
            assert first["success"] and first["metadata"]["mode"] == "active"
            second = await plugin.handle_goqbot_gateway(outbound)
            assert not second["success"] and "频率限制" in second["error"]

            await ws.close()
            await wait_for(lambda: host.states[-1]["ready"] is False)
            serve_task.cancel()
        finally:
            await session.close()
            await plugin.on_unload()

    asyncio.run(scenario())


def test_bootstrap_failure_disconnects_and_retries():
    async def scenario():
        logic = FakeGoqbotLogic()
        logins = {"n": 0}
        original = logic.handle

        def handle(req):
            if req["action"] == "get_login_info":
                logins["n"] += 1
                if logins["n"] == 1:  # 第一次拿不到 self_id
                    return {"status": "ok", "retcode": 0, "data": {"user_id": 0}, "echo": req["echo"]}
            return original(req)

        logic.handle = handle
        connections = {"n": 0}

        async def ws_handler(request):
            connections["n"] += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str(json.dumps(logic.handle(json.loads(m.data))))
            return ws

        app = web.Application()
        app.router.add_get("/", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        host = FakeHost()
        plugin = make_plugin(host, {"mode": "正向", "address": f"ws://127.0.0.1:{port}", "access_token": ""})
        await plugin.on_load()
        try:
            await wait_for(lambda: any(s["ready"] for s in host.states))
            assert connections["n"] == 2 and logins["n"] == 2
        finally:
            await plugin.on_unload()
            await runner.cleanup()

    asyncio.run(scenario())


def test_old_goqbot_version_warns(caplog):
    async def scenario():
        logic = FakeGoqbotLogic(version="1.10.1")
        host = FakeHost()
        port = free_port()
        plugin = make_plugin(host, {"mode": "反向", "address": f"ws://127.0.0.1:{port}/onebot/v11/ws"})
        await plugin.on_load()
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(f"ws://127.0.0.1:{port}/onebot/v11/ws?access_token={TOKEN}")

            async def serve():
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await ws.send_str(json.dumps(logic.handle(json.loads(m.data))))

            task = asyncio.create_task(serve())
            await wait_for(lambda: any(s["ready"] for s in host.states))
            await ws.close()
            task.cancel()
        finally:
            await session.close()
            await plugin.on_unload()

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())
    assert any("passive_only" in r.getMessage() for r in caplog.records)
