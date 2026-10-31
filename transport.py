"""与 goqbot 之间的 OneBot V11 WebSocket 传输层。

- ``ForwardTransport``：插件作为客户端连接 goqbot 正向 ws，断线自动重连。
- ``ReverseTransport``：插件作为服务端，等待 goqbot 的 ws-reverse（universal）连入。

两者共用同一套动作调用（echo 匹配）与事件分发逻辑；同一时刻只维护一条活跃连接。
事件按到达顺序串行交给上层处理，保证消息顺序。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Union
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
from uuid import uuid4

import asyncio
import contextlib
import hmac
import json
import re

import aiohttp
from aiohttp import web

from .config import ConnectionConfig

OnOpen = Callable[[str], Awaitable[None]]
"""连接建立回调，参数为握手阶段得知的 self_id（未知时为空串）。"""
OnClose = Callable[[], Awaitable[None]]
OnEvent = Callable[[Dict[str, Any]], Awaitable[None]]

WSLike = Union[aiohttp.ClientWebSocketResponse, web.WebSocketResponse]


class TransportNotConnected(RuntimeError):
    """goqbot 当前未连接。"""


def redact(text: str, token: str) -> str:
    """去掉日志文本中的访问令牌。"""

    if token:
        text = text.replace(token, "***")
    return re.sub(r"(?i)(access_token=)[^&\s'\"]+", r"\1***", text)


class _BaseTransport:
    """正/反向共用的连接状态、动作调用与事件分发。"""

    def __init__(self, logger: Any, on_open: OnOpen, on_close: OnClose, on_event: OnEvent) -> None:
        self._logger = logger
        self._on_open = on_open
        self._on_close = on_close
        self._on_event = on_event
        self._config: Optional[ConnectionConfig] = None
        self._ws: Optional[WSLike] = None
        self._pending: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._event_queue: Optional[asyncio.Queue[Optional[Dict[str, Any]]]] = None
        self._event_worker: Optional[asyncio.Task[None]] = None
        self._bootstrap_task: Optional[asyncio.Task[None]] = None
        self.log_raw = False

    # ── 公共接口 ────────────────────────────────────────────────

    def configure(self, config: ConnectionConfig) -> None:
        self._config = config

    @property
    def connected(self) -> bool:
        ws = self._ws
        return ws is not None and not ws.closed

    async def call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """发送一个 OneBot 动作并等待同 echo 的响应。"""

        ws = self._ws
        config = self._config
        if ws is None or ws.closed or config is None:
            raise TransportNotConnected("goqbot 未连接")
        echo = uuid4().hex
        future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            payload = json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            async with self._send_lock:
                await ws.send_str(payload)
            return await asyncio.wait_for(future, timeout=config.action_timeout_sec)
        finally:
            self._pending.pop(echo, None)

    async def start(self) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    # ── 连接生命周期 ───────────────────────────────────────────

    async def _serve_connection(self, ws: WSLike, self_id_hint: str) -> str:
        """接管一条已建立的连接，直到它断开；返回断开原因。"""

        self._ws = ws
        self._event_queue = asyncio.Queue()
        self._event_worker = asyncio.create_task(self._drain_events(self._event_queue), name="goqbot.events")
        # 上线流程（拉取 self_id 等）需要调用动作，必须与收包循环并发，否则会等不到响应。
        self._bootstrap_task = asyncio.create_task(self._safe_on_open(ws, self_id_hint), name="goqbot.bootstrap")
        reason = "连接已结束"
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch_text(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    reason = f"对端关闭连接（code={ws.close_code}）"
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    reason = f"连接异常: {ws.exception()!r}"
                    break
        finally:
            await self._teardown_connection(ws)
        return reason

    async def _teardown_connection(self, ws: WSLike) -> None:
        if self._ws is ws:
            self._ws = None
        for task in (self._bootstrap_task,):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._bootstrap_task = None
        queue, worker = self._event_queue, self._event_worker
        self._event_queue, self._event_worker = None, None
        if queue is not None:
            queue.put_nowait(None)
        if worker is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(worker, timeout=5)
        self._fail_pending("连接已断开")
        with contextlib.suppress(Exception):
            await ws.close()
        try:
            await self._on_close()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(f"[goqbot] 断开回调异常: {exc}")

    async def _safe_on_open(self, ws: WSLike, self_id_hint: str) -> None:
        try:
            await self._on_open(self_id_hint)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 断开这条连接：正向会按重连间隔重试，反向由 goqbot 的 reconnect-interval 重连
            self._logger.error(f"[goqbot] 连接建立后的初始化失败，将断开并重试: {exc}")
            with contextlib.suppress(Exception):
                await ws.close()

    def _dispatch_text(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            self._logger.debug("[goqbot] 收到无法解析的报文，已忽略")
            return
        if not isinstance(payload, dict):
            return
        echo = payload.get("echo")
        if echo is not None and "post_type" not in payload:
            future = self._pending.get(str(echo))
            if future is not None and not future.done():
                future.set_result(payload)
            return
        if self.log_raw:
            self._logger.debug(f"[goqbot] 事件: {raw[:2000]}")
        if self._event_queue is not None:
            self._event_queue.put_nowait(payload)

    async def _drain_events(self, queue: "asyncio.Queue[Optional[Dict[str, Any]]]") -> None:
        while True:
            payload = await queue.get()
            if payload is None:
                return
            try:
                await self._on_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._logger.error(f"[goqbot] 处理事件失败: {exc}", exc_info=True)

    def _fail_pending(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportNotConnected(message))
        self._pending.clear()


class ForwardTransport(_BaseTransport):
    """正向：插件主动连接 goqbot。"""

    def __init__(self, logger: Any, on_open: OnOpen, on_close: OnClose, on_event: OnEvent) -> None:
        super().__init__(logger, on_open, on_close, on_event)
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._stopping = False

    def describe(self) -> str:
        return f"正向 {redact(self._config.address, self._config.access_token) if self._config else '?'}"

    def _connect_url(self) -> str:
        assert self._config is not None
        url = self._config.address
        if not self._config.access_token:
            return url
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query))
        query["access_token"] = self._config.access_token
        return urlunparse(parsed._replace(query=urlencode(query)))

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(self._connect_loop(), name="goqbot.forward")

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _connect_loop(self) -> None:
        while not self._stopping:
            config = self._config
            if config is None:
                return
            headers = {"Authorization": f"Bearer {config.access_token}"} if config.access_token else {}
            display = redact(config.address, config.access_token)
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=10)
                async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                    async with session.ws_connect(
                        self._connect_url(),
                        heartbeat=config.heartbeat_interval_sec,
                        max_msg_size=100 * 1024 * 1024,
                    ) as ws:
                        self._logger.info(f"[goqbot] 已连接 {display}")
                        reason = await self._serve_connection(ws, "")
                        if not self._stopping:
                            self._logger.warning(f"[goqbot] 连接断开 {display}：{reason}")
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as exc:
                hint = "访问令牌不一致" if exc.status in (401, 403) else "请检查地址是否为 goqbot 正向 ws"
                self._logger.warning(f"[goqbot] 握手失败 {display}（HTTP {exc.status}，{hint}）")
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(f"[goqbot] 连接失败 {display}：{redact(str(exc), config.access_token)}")
            if self._stopping:
                return
            await asyncio.sleep(config.reconnect_delay_sec)


class ReverseTransport(_BaseTransport):
    """反向：插件监听端口，goqbot 通过 ws-reverse universal 连入。"""

    def __init__(self, logger: Any, on_open: OnOpen, on_close: OnClose, on_event: OnEvent) -> None:
        super().__init__(logger, on_open, on_close, on_event)
        self._runner: Optional[web.AppRunner] = None
        self._current: Optional[web.WebSocketResponse] = None

    def describe(self) -> str:
        c = self._config
        return f"反向 {c.address}" if c else "反向 ?"

    async def start(self) -> None:
        if self._runner is not None:
            return
        config = self._config
        assert config is not None
        app = web.Application()
        listen = config.parse_address()
        app.router.add_get(listen.path, self._handle)
        runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await runner.setup()
        site = web.TCPSite(runner, listen.host, listen.port)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            raise RuntimeError(f"反向 WebSocket 监听失败（{self.describe()}）：{exc}") from exc
        self._runner = runner
        self._logger.info(f"[goqbot] 等待 goqbot 连入：{self.describe()}")

    async def stop(self) -> None:
        current = self._current
        if current is not None:
            with contextlib.suppress(Exception):
                await current.close()
        runner, self._runner = self._runner, None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()

    def _authorized(self, request: web.Request) -> bool:
        token = self._config.access_token if self._config else ""
        if not token:
            return True
        candidates = [request.query.get("access_token", "")]
        auth = request.headers.get("Authorization", "")
        for prefix in ("Bearer ", "Token "):  # goqbot 反向使用 "Token xxx"
            if auth.startswith(prefix):
                candidates.append(auth[len(prefix):].strip())
        return any(c and hmac.compare_digest(c, token) for c in candidates)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            self._logger.warning(f"[goqbot] 拒绝来自 {request.remote} 的连接：访问令牌不一致")
            return web.Response(status=401, text="unauthorized")
        role = request.headers.get("X-Client-Role", "Universal")
        if role.lower() != "universal":
            self._logger.warning(f"[goqbot] 仅支持 universal 反向连接，已拒绝 X-Client-Role={role}")
            return web.Response(status=400, text="only universal role is supported")

        assert self._config is not None
        ws = web.WebSocketResponse(heartbeat=self._config.heartbeat_interval_sec, max_msg_size=100 * 1024 * 1024)
        await ws.prepare(request)
        previous = self._current
        if previous is not None and not previous.closed:
            self._logger.warning("[goqbot] 新的 goqbot 连接已建立，旧连接将被关闭")
            with contextlib.suppress(Exception):
                await previous.close()
            # 等旧连接完成清理，避免新旧连接的上线/下线状态交错
            for _ in range(50):
                if self._ws is None:
                    break
                await asyncio.sleep(0.05)
        self._current = ws
        self_id = request.headers.get("X-Self-ID", "").strip()
        self._logger.info(f"[goqbot] goqbot 已连入（{request.remote}，self_id={self_id or '?'}）")
        reason = await self._serve_connection(ws, self_id)
        if self._current is ws:
            self._current = None
        if self._runner is not None:
            self._logger.warning(f"[goqbot] goqbot 反向连接断开：{reason}")
        return ws


def build_transport(
    config: ConnectionConfig, logger: Any, on_open: OnOpen, on_close: OnClose, on_event: OnEvent
) -> _BaseTransport:
    cls = ReverseTransport if config.is_reverse else ForwardTransport
    transport = cls(logger, on_open, on_close, on_event)
    transport.configure(config)
    return transport
