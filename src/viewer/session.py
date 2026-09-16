# -*- coding: utf-8 -*-
"""Viewer 会话：连接中继、解密、把变化块拼成完整画面。

本模块**不含界面**，只负责传输与还原，方便：
* 界面层（main.py）自由实现
* 无头集成测试直接驱动它验证整条 Phase 1 链路

画面还原
--------
维护一块 ``(H, W, 3)`` 的 numpy 画布：

* **关键帧**：整屏替换画布
* **普通帧**：按包里的 ``(ty, tx, ax, ay)`` 把 atlas 上的块贴回画布对应位置

坐标以「块」为单位，乘 tile_size 得像素位置。因为输入事件用的是**归一化坐标**，
所以 Agent 端是否降采样都不影响操作正确性。
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Tuple

import numpy as np
from PIL import Image

from common import config as config_mod
from common import protocol, wssession
from common.wssession import HandshakeError

PING_INTERVAL = 5.0            # 保活 + 让 Agent 的空闲检测知道 Viewer 还在
STATS_INTERVAL = 2.0
INPUT_QUEUE_MAX = 256          # 输入事件积压上限，超出丢最旧的（鼠标移动丢旧的没关系）


class ViewerSession:
    """控制端会话。

    ``on_frame(canvas, meta)`` 在事件循环线程里被调用；
    界面层需要自己把数据搬到 Qt 主线程（``main.py`` 用信号做这件事）。
    """

    def __init__(self, cfg: config_mod.AppConfig,
                 on_frame: Optional[Callable[[np.ndarray, Dict[str, Any]], None]] = None,
                 on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None,
                 on_clipboard: Optional[Callable[[str], None]] = None,
                 log: Callable[[str], None] = print,
                 enable_input: bool = False):
        self.cfg = cfg
        self.on_frame = on_frame
        self.on_status = on_status
        self.on_state = on_state
        self.on_clipboard = on_clipboard
        self.log = log
        self.enable_input = enable_input

        self.canvas: Optional[np.ndarray] = None
        self.tile_size = cfg.agent.tile_size
        self.peer: Optional[wssession.PeerInfo] = None
        self.stop_flag = threading.Event()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[wssession.Session] = None
        self._input_queue: Deque[Tuple[int, bytes]] = deque(maxlen=INPUT_QUEUE_MAX)
        self._input_evt: Optional[asyncio.Event] = None
        self._need_keyframe = True
        self._last_pong = 0.0

        self.stats: Dict[str, Any] = {
            "frames": 0, "keyframes": 0, "bytes": 0,
            "decode_ms": 0.0, "started": time.time(), "last_frame_at": 0.0,
        }

    # ------------------------------------------------------------ 生命周期

    async def run(self) -> None:
        """连接 + 自动重连，直到 ``stop_flag`` 被设置。"""
        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self.stop_flag.is_set():
            try:
                self._state("connecting")
                await self._run_once()
                backoff = 1.0
            except HandshakeError as e:
                self.log("[会话] 连接失败：%s" % e)
                self._state("failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log("[会话] 异常退出：%s: %s" % (type(e).__name__, e))
                self._state("failed")

            if self.stop_flag.is_set():
                break
            self._state("reconnecting")
            self.log("[会话] %.1f 秒后重连……" % backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(30.0, backoff * 2)
        self._state("stopped")

    def stop(self) -> None:
        self.stop_flag.set()

    def _state(self, name: str) -> None:
        if self.on_state:
            try:
                self.on_state(name)
            except Exception:
                pass

    # ------------------------------------------------------------ 单次会话

    async def _run_once(self) -> None:
        v = self.cfg.viewer
        sess = await wssession.connect(
            self.cfg, protocol.ROLE_VIEWER,
            hello_extra={"viewer": {"window": [v.window_width, v.window_height],
                                    "scale_mode": v.scale_mode}},
            log=self.log,
        )
        self._session = sess
        self.peer = sess.peer
        self.tile_size = int((sess.peer.screen or {}).get("tile_size") or self.cfg.agent.tile_size)
        self.canvas = None
        self._need_keyframe = True
        self._input_evt = asyncio.Event()
        self._state("connected")

        screen = sess.peer.screen or {}
        ro = sess.peer.readonly
        ro_txt = "未知" if ro is None else ("是（Agent 只上传画面，不会注入键鼠）" if ro
                                          else "否（Agent 允许注入键鼠）")
        self.log("[会话] 对端屏幕 %sx%s，分块 %s，Agent 只读模式=%s"
                 % (screen.get("source_width", "?"), screen.get("source_height", "?"),
                    self.tile_size, ro_txt))
        if ro and self.enable_input:
            self.log("[警告] 你启用了键鼠注入，但 Agent 处于只读模式，注入会被忽略。"
                     "需要在 Agent 端把 allow_input 设为 true 并重启。")

        tasks = [
            asyncio.create_task(self._recv_loop(sess), name="recv"),
            asyncio.create_task(self._input_loop(sess), name="input"),
            asyncio.create_task(self._keepalive_loop(sess), name="keepalive"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    raise exc
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await sess.close()
            self._session = None

    # ------------------------------------------------------------ 接收

    async def _recv_loop(self, sess: wssession.Session) -> None:
        """收帧并还原画面。"""
        stat_frames = 0
        stat_bytes = 0
        stat_decode = 0.0
        last_stat = time.time()

        while True:
            mtype, payload = await sess.recv_peer_message()

            if mtype == protocol.MSG_FRAME:
                t0 = time.perf_counter()
                try:
                    pkt = protocol.decode_frame_packet(payload)
                    self._apply_packet(pkt)
                except Exception as e:
                    self.log("[画面] 帧包处理失败：%s: %s" % (type(e).__name__, e))
                    self._need_keyframe = True
                else:
                    dt = (time.perf_counter() - t0) * 1000
                    stat_frames += 1
                    stat_bytes += len(payload)
                    stat_decode += dt
                    self.stats["frames"] += 1
                    self.stats["bytes"] += len(payload)
                    self.stats["last_frame_at"] = time.time()
                    if pkt.get("keyframe"):
                        self.stats["keyframes"] += 1

            elif mtype == protocol.MSG_CTRL:
                msg = protocol.parse_ctrl(payload)
                kind = msg.get("t")
                if kind == protocol.CTRL_PONG:
                    ts = msg.get("ts")
                    if isinstance(ts, (int, float)):
                        self._last_pong = (time.time() - ts) * 1000.0
                elif kind == protocol.CTRL_CLIPBOARD:
                    text = protocol.clipboard_text_from(msg)
                    if text and self.on_clipboard:
                        try:
                            self.on_clipboard(text)
                        except Exception as e:
                            self.log("[剪贴板] 回调出错：%s: %s" % (type(e).__name__, e))
                # CTRL_STATS 等其余控制消息暂不需要处理

            else:
                self.log("[会话] 收到未处理的消息类型 %s" % protocol.msg_name(mtype))

            # 状态上报必须每轮都检查（放在 continue 之外）
            now = time.time()
            if now - last_stat >= STATS_INTERVAL:
                elapsed = max(1e-6, now - last_stat)
                if self.on_status:
                    try:
                        self.on_status({
                            "fps": stat_frames / elapsed,
                            "mbps": stat_bytes * 8 / elapsed / 1e6,
                            "decode_ms": stat_decode / max(1, stat_frames),
                            "rtt_ms": self._last_pong,
                            "total_frames": self.stats["frames"],
                            "keyframes": self.stats["keyframes"],
                            "size": (self.canvas.shape[1], self.canvas.shape[0])
                                    if self.canvas is not None else None,
                        })
                    except Exception:
                        pass
                stat_frames = stat_bytes = 0
                stat_decode = 0.0
                last_stat = now

    def _apply_packet(self, pkt: Dict[str, Any]) -> None:
        """把帧包贴到画布上。"""
        ts = self.tile_size
        raw = pkt["jpeg"]
        atlas = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))
        aw, ah = pkt["atlas_w"], pkt["atlas_h"]

        if pkt.get("keyframe"):
            h, w = atlas.shape[:2]
            if self.canvas is None or self.canvas.shape[:2] != (h, w):
                self.canvas = np.empty((h, w, 3), dtype=np.uint8)
            np.copyto(self.canvas, atlas)
            self._emit_frame()
            return

        if self.canvas is None:
            # 还没收到关键帧，先请求一个；否则贴图没有基准
            self._need_keyframe = True
            return

        ch, cw = self.canvas.shape[:2]
        for ty, tx, ax, ay in pkt["tiles"]:
            sy, sx = ay * ts, ax * ts
            dy, dx = ty * ts, tx * ts
            # 越界保护：包损坏或尺寸变化时不能让 numpy 抛异常
            if dy + ts > ch or dx + ts > cw or sy + ts > ah or sx + ts > aw:
                continue
            self.canvas[dy:dy + ts, dx:dx + ts] = atlas[sy:sy + ts, sx:sx + ts]

        self._emit_frame()

    def _emit_frame(self) -> None:
        if self.on_frame and self.canvas is not None:
            try:
                # 必须复制：画布在下一帧会被**原地修改**，而回调（界面层）
                # 通常异步消费这份数据。不复制就会出现撕裂/错帧。
                self.on_frame(self.canvas.copy(), {"tile_size": self.tile_size})
            except Exception as e:
                self.log("[画面] 回调出错：%s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------ 发送

    def _enqueue(self, msg_type: int, payload: bytes) -> None:
        """线程安全地排队一条输入事件（从 Qt 线程或任意线程调用）。"""
        self._input_queue.append((msg_type, payload))
        loop = self._loop
        evt = self._input_evt
        if loop is not None and evt is not None:
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                pass

    async def _input_loop(self, sess: wssession.Session) -> None:
        """把排队的输入事件加密发出去。"""
        while True:
            if not self._input_queue:
                assert self._input_evt is not None
                self._input_evt.clear()
                await self._input_evt.wait()
                continue
            mtype, payload = self._input_queue.popleft()
            await sess.send(mtype, payload)

    async def _keepalive_loop(self, sess: wssession.Session) -> None:
        """定期 ping：既测 RTT，也让 Agent 的空闲检测知道 Viewer 还在。

        用 0.5 秒的小步进轮询，这样「请求关键帧」能马上发出去，
        而不是要等到下一次 ping。
        """
        next_ping = 0.0
        while True:
            if self._need_keyframe:
                self._need_keyframe = False
                await sess.send_ctrl(protocol.CTRL_KEYFRAME)
            now = time.time()
            if now >= next_ping:
                await sess.send_ctrl(protocol.CTRL_PING, ts=time.time())
                next_ping = now + PING_INTERVAL
            await asyncio.sleep(0.5)

    def request_keyframe(self) -> None:
        self._need_keyframe = True
        loop, evt = self._loop, self._input_evt
        if loop and evt:
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                pass

    # ---------------- 键鼠（Phase 2 才注入，Phase 1 只排队不生效）----------------

    def send_mouse_move(self, x: float, y: float) -> None:
        if self.enable_input and self._session:
            self._enqueue(protocol.MSG_INPUT, protocol.input_mouse(x, y))

    def send_mouse_button(self, button: int, down: bool, x: float, y: float) -> None:
        if self.enable_input and self._session:
            self._enqueue(protocol.MSG_INPUT, protocol.input_button(button, down, x, y))

    def send_wheel(self, delta: int, x: float, y: float) -> None:
        if self.enable_input and self._session:
            self._enqueue(protocol.MSG_INPUT, protocol.input_wheel(delta, x, y))

    def send_key(self, scancode: int, down: bool, extended: bool = False) -> None:
        if self.enable_input and self._session:
            self._enqueue(protocol.MSG_INPUT, protocol.input_key(scancode, down, extended))

    # ---------------- 剪贴板（不需要 enable_input：它是双向同步，不是操作目标机）----------------

    def send_clipboard(self, text: str) -> None:
        """把本机剪贴板内容同步给对端（从 Qt 主线程调用，线程安全）。

        注意：剪贴板同步**不依赖 enable_input** —— 它不属于「操作目标机」，
        只是双向搬运文本。想让对端完全看不到你的剪贴板，请关掉 clipboard_sync。
        """
        if not self.cfg.viewer.clipboard_sync or not self._session or not text:
            return
        if len(text) > protocol.MAX_CLIPBOARD_LEN:
            self.log("[剪贴板] 内容过大（%d 字符），跳过" % len(text))
            return
        self._enqueue(protocol.MSG_CTRL, protocol.ctrl_clipboard(text))

    def send_bye(self) -> None:
        if not self._session:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._session.send_ctrl(protocol.CTRL_BYE), loop)
        except RuntimeError:
            pass
