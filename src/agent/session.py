# -*- coding: utf-8 -*-
"""Agent 会话：采集 → 差分 → 编码 → 加密发送，并处理来自 Viewer 的控制消息。

线程模型
--------
::

    采集/编码线程（阻塞）          事件循环（asyncio）
    ─────────────────────         ──────────────────────
    grab → diff → atlas → JPEG  →  Outbox  →  发送协程 → WebSocket
                                                  ↑
                                    接收协程（处理 CTRL / INPUT）

采集与编码是 CPU 密集且阻塞的，所以放在独立线程里跑；
结果通过线程安全的 ``Outbox`` 交给事件循环发送。

丢帧一致性（容易忽略但很重要）
------------------------------
带宽不足时我们会主动丢帧（宁可掉帧也不积压延迟）。但**丢帧不等于可以忘记它**：
一个更新包只含「相对上一帧的变化块」，丢了就意味着那几个块在 Viewer 上永久缺失，
画面会一直残缺到下一次关键帧。

所以 ``Outbox`` 丢弃最旧的包时，会把它涉及的块返回给采集线程，
采集线程把这些块并回「待补发集合」，在下一帧重新发送。这样画面能自愈，
而且**不需要为此发整屏关键帧**（拥塞时发大帧只会雪上加霜）。
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from PIL import Image

from common import config as config_mod
from common import protocol, wssession
from common.wssession import HandshakeError

from . import capture as capture_mod
from . import clipboard as clipboard_mod
from . import encoder as encoder_mod
from . import input as input_mod

LOOP_SLEEP_IDLE = 0.002        # 画面静止时的轮询间隔，避免空转烧 CPU


class Outbox:
    """线程安全的发送邮箱。

    容量满时**丢弃最旧的包**（保留最新的画面状态），并把被丢包涉及的块
    交还给采集线程补发 —— 见模块文档里「丢帧一致性」一节。
    """

    def __init__(self, maxlen: int, loop: asyncio.AbstractEventLoop):
        self._items: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._maxlen = max(1, maxlen)
        self._loop = loop
        self._event = asyncio.Event()
        self.dropped = 0
        self.sent = 0

    def put(self, packet: bytes, tiles: List[encoder_mod.Tile]) -> List[encoder_mod.Tile]:
        """放入一个包。返回「需要补发」的块列表（因为旧包被丢弃）。"""
        stale: List[encoder_mod.Tile] = []
        with self._lock:
            if len(self._items) >= self._maxlen:
                _old_packet, old_tiles = self._items.popleft()
                stale = list(old_tiles)
                self.dropped += 1
            self._items.append((packet, tiles))
        self._loop.call_soon_threadsafe(self._event.set)
        return stale

    async def get(self):
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
            self._event.clear()
            await self._event.wait()

    def depth(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class AgentSession:
    """受控端会话。用法：``await AgentSession(cfg).run()``。"""

    def __init__(self, cfg: config_mod.AppConfig, log=print):
        self.cfg = cfg
        self.log = log
        self.stop_flag = threading.Event()
        self._capturer: Optional[capture_mod.Capturer] = None

        # 采集线程与发送协程之间的共享状态
        self._outbox: Optional[Outbox] = None
        self._producer_stop = threading.Event()
        self._force_keyframe = threading.Event()
        self._quality_override: Optional[int] = None
        self._last_viewer_activity = time.time()

        # ---- 保命措施 ----
        self._injector: Optional[input_mod.InputInjector] = None
        self._hotkey: Optional[input_mod.PanicHotkey] = None
        self._panic = threading.Event()        # 急停热键被按下

        # ---- 剪贴板同步 ----
        self._clipboard: Optional[clipboard_mod.ClipboardWatcher] = None

        self.stats: Dict[str, Any] = {
            "frames_captured": 0,
            "frames_sent": 0,
            "bytes_sent": 0,
            "keyframes": 0,
            "dropped": 0,
            "input_ignored": 0,
            "input_injected": 0,
            "input_blocked": 0,
            "started": time.time(),
        }

    # ------------------------------------------------------------ 入口

    async def run(self) -> None:
        """连接 + 自动重连，直到 ``stop_flag`` 被设置。"""
        backoff = 1.0
        while not self.stop_flag.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except HandshakeError as e:
                self.log("[会话] 连接失败：%s" % e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log("[会话] 异常退出：%s: %s" % (type(e).__name__, e))
            if self.stop_flag.is_set():
                break
            self.log("[会话] %.1f 秒后重连……" % backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(30.0, backoff * 2)

    def stop(self) -> None:
        self.stop_flag.set()

    # ------------------------------------------------------------ 单次会话

    def _ensure_capturer(self) -> capture_mod.Capturer:
        if self._capturer is None:
            a = self.cfg.agent
            self._capturer = capture_mod.make_capturer(a.capture_backend, a.output, self.log)
        return self._capturer

    # ------------------------------------------------------------ 保命措施

    def _on_panic(self) -> None:
        """急停热键的回调（在热键线程里执行）。

        做三件事：暂停注入 → 松开所有按键（避免远端卡键）→ **彻底停止 Agent**。

        这里刻意连重连也一起停掉：如果只是断开会话，Agent 会自动重连、
        Viewer 一回来注入就恢复了，那急停就失去了意义。
        急停之后再要用，必须手动重新启动 Agent —— 这是有意的。
        """
        self.log("[保命] 🔴 急停：停止注入、松开按键、停止 Agent（需手动重启才能再用）")
        if self._injector is not None:
            self._injector.pause("急停热键被按下")
            self._injector.release_all()
        self._panic.set()
        self.stop_flag.set()

    def _setup_injection(self, cap: capture_mod.Capturer) -> None:
        """按配置准备注入器与急停热键。只读模式下什么都不做。"""
        a = self.cfg.agent
        if not a.allow_input:
            return

        rect = cap.monitor_rect()
        if rect is None:
            self.log("[注入] ⚠️ 无法确定采集显示器的虚拟桌面位置，键鼠注入可能偏位。")
            self.log("[注入] 当前枚举到的显示器：")
            for line in capture_mod.describe_monitors().splitlines():
                self.log(line)
            self.log("[注入] 提示：多半是多块显示器**分辨率完全相同**导致无法区分。")
            self.log("       可以先把 config 的 output 留空（采集主屏）再开注入。")
            rect = (0, 0, cap.width, cap.height)
        else:
            self.log("[注入] 显示器区域 left=%d top=%d %dx%d（已做多显示器偏移校正）" % rect)

        dry = bool(a.input_dry_run or os.environ.get("ALSPD_INPUT_DRY_RUN") == "1")
        if dry:
            self.log("[注入] ⚠️ 演练模式（dry-run）：只记录应该注入什么，"
                     "**不会真的操作本机键鼠**。")
        self._injector = input_mod.InputInjector(
            rect, log=self.log, dry_run=dry,
            activity_guard=a.input_activity_guard,
            guard_threshold=a.input_guard_threshold,
            guard_auto_resume=a.input_guard_auto_resume,
        )

        self._hotkey = input_mod.PanicHotkey(a.panic_hotkey, self._on_panic, log=self.log)
        ok = self._hotkey.start()
        if not ok:
            self.log("[急停] ⚠️ 急停热键不可用 —— 仍受「空闲自动断连」与「本机活动检测」保护，"
                     "但建议换一个热键组合。")

    def _teardown_injection(self) -> None:
        if self._injector is not None:
            try:
                self._injector.release_all()
            except Exception:
                pass
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
            self._hotkey = None

    # ------------------------------------------------------------ 剪贴板

    def _setup_clipboard(self, sess: wssession.Session,
                         loop: asyncio.AbstractEventLoop) -> None:
        a = self.cfg.agent
        if not a.clipboard_sync:
            self.log("[剪贴板] 已禁用（clipboard_sync = false）")
            return

        def on_change(text: str) -> None:
            # 这个回调在剪贴板监听线程里执行，必须**线程安全地**投递给事件循环。
            # 剪贴板变化频率很低（几分钟一次），所以直接 run_coroutine_threadsafe 就够了。
            try:
                asyncio.run_coroutine_threadsafe(
                    sess.send_ctrl(protocol.CTRL_CLIPBOARD, text=text), loop)
            except Exception as e:
                self.log("[剪贴板] 投递失败：%s: %s" % (type(e).__name__, e))

        self._clipboard = clipboard_mod.ClipboardWatcher(
            on_change, log=self.log, interval=a.clipboard_interval)
        self._clipboard.start()

    def _teardown_clipboard(self) -> None:
        if self._clipboard is not None:
            try:
                self._clipboard.stop()
            except Exception:
                pass
            # 先把统计快照下来 —— _log_stats 在这之后才调用，
            # 直接引用会被清空导致统计永远打不出来
            self.stats["clipboard"] = dict(self._clipboard.stats)
            self._clipboard = None

    async def _run_once(self) -> None:
        a = self.cfg.agent
        cap = self._ensure_capturer()
        self._setup_injection(cap)
        ts = a.tile_size

        # 对齐到分块的整数倍（1440 不是 64 的倍数）
        W = (cap.width // ts) * ts
        H = (cap.height // ts) * ts
        if W <= 0 or H <= 0:
            raise RuntimeError("屏幕尺寸 %dx%d 小于一个分块" % (cap.width, cap.height))

        dst_w, dst_h, factor = encoder_mod.target_size(W, H, a.target_width, ts)
        if factor > 1:
            W, H = dst_w, dst_h
            self.log("[会话] 降采样 1/%d（仅整数倍）-> %dx%d" % (factor, W, H))

        readonly = self._injector is None    # 注入器没建起来就一律当只读
        self.log("[会话] 屏幕 %dx%d，分块 %d，%s"
                 % (W, H, ts,
                    "只读模式（不注入键鼠）" if readonly
                    else "键鼠注入已启用 —— 急停热键 %s" % a.panic_hotkey))

        sess = await wssession.connect(
            self.cfg, protocol.ROLE_AGENT,
            hello_extra={
                "screen": {"width": W, "height": H, "source_width": cap.width,
                           "source_height": cap.height, "tile_size": ts,
                           "factor": factor},
                "readonly": readonly,
                "agent": {"version": "0.1", "capture": cap.name},
            },
            log=self.log,
        )

        loop = asyncio.get_running_loop()
        self._outbox = Outbox(a.send_queue_max, loop)
        self._producer_stop.clear()
        self._force_keyframe.set()          # 首帧必须是关键帧
        self._last_viewer_activity = time.time()

        producer = threading.Thread(
            target=self._produce_frames,
            args=(cap, W, H, factor, ts, loop),
            name="capture", daemon=True,
        )
        producer.start()

        # 剪贴板要在配对成功之后再起 —— 它需要 sess 来发送
        self._setup_clipboard(sess, loop)

        tasks = [
            asyncio.create_task(self._send_loop(sess), name="send"),
            asyncio.create_task(self._recv_loop(sess), name="recv"),
            asyncio.create_task(self._watchdog(sess), name="watchdog"),
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
            self._producer_stop.set()
            producer.join(timeout=3.0)
            # 断线时必须松开所有按键，否则远端会留下「卡住的按键」
            self._teardown_injection()
            self._teardown_clipboard()
            await sess.close()
            self._log_stats()

    def _log_stats(self) -> None:
        s = self.stats
        elapsed = max(1e-6, time.time() - s["started"])
        self.log("[统计] 采集 %d 帧 / 发送 %d 帧 / %.2f MB / 丢弃 %d 帧 / 关键帧 %d 次 / 平均 %.2f Mbps"
                 % (s["frames_captured"], s["frames_sent"], s["bytes_sent"] / 1048576.0,
                    s["dropped"], s["keyframes"], s["bytes_sent"] * 8 / elapsed / 1e6))
        if s["input_ignored"]:
            self.log("[统计] 只读模式下忽略了 %d 个键鼠事件（未注入）" % s["input_ignored"])
        if s["input_injected"] or s["input_blocked"]:
            self.log("[统计] 键鼠：注入 %d 次，被保护逻辑拦下 %d 次"
                     % (s["input_injected"], s["input_blocked"]))
        if self._injector is not None:
            st = self._injector.stats
            self.log("[统计] 注入明细：移动 %d / 点击 %d / 滚轮 %d / 按键 %d，"
                     "本机活动检测触发 %d 次，SendInput 失败 %d 次"
                     % (st["mouse_moves"], st["buttons"], st["wheels"], st["keys"],
                        st["guard_trips"], st["send_failures"]))
        if self._panic.is_set():
            self.log("[统计] ⚠️ 本次会话由急停热键终止")
        cst = s.get("clipboard")
        if cst:
            self.log("[统计] 剪贴板：发送 %d 次 / 应用 %d 次 / 过大跳过 %d / 写入失败 %d"
                     % (cst["sent"], cst["applied"],
                        cst["skipped_too_long"], cst["write_failed"]))

    # ------------------------------------------------------------ 采集线程

    def _produce_frames(self, cap, W, H, factor, ts, loop) -> None:
        """采集线程主体。阻塞式循环，退出条件由 ``_producer_stop`` 控制。"""
        a = self.cfg.agent
        quality = encoder_mod.AdaptiveQuality(a.jpeg_quality_min, a.jpeg_quality_max,
                                              queue_max=a.send_queue_max)
        prev: Optional[np.ndarray] = None
        pending: Set[Tuple[int, int]] = set()      # 因丢帧需要补发的块
        last_keyframe = 0.0
        dirty_since_keyframe = False               # 上次关键帧之后有没有发过更新
        last_stat = time.time()
        stat_frames = 0
        stat_bytes = 0
        max_fps = max(1.0, float(a.max_fps))
        frame_interval = 1.0 / max_fps
        sleep_idle = LOOP_SLEEP_IDLE

        while not self._producer_stop.is_set():
            t_start = time.perf_counter()

            want_keyframe = self._force_keyframe.is_set()
            # 需要关键帧时必须**强制取帧**：画面静止时 dxcam 会一直返回 None，
            # 那样新连上来的 Viewer 就永远看不到首屏。
            rgb = cap.grab(force=want_keyframe)
            if rgb is None:
                # 画面没有变化。静止时几乎零开销 —— 这是 dxcam 相对 mss 的最大优势
                time.sleep(sleep_idle)
                continue

            cur = rgb[:H, :W]
            if not cur.flags["C_CONTIGUOUS"]:
                cur = np.ascontiguousarray(cur)

            try:
                q = self._quality_override if self._quality_override is not None else quality.quality

                if want_keyframe or prev is None or prev.shape != cur.shape:
                    # 关键帧请求 / 首帧 / 尺寸变化 -> 整屏重发
                    self._force_keyframe.clear()
                    stat_bytes += self._push_keyframe(cur, q)
                    last_keyframe = time.time()
                    dirty_since_keyframe = False
                    prev = cur
                    pending.clear()
                    stat_frames += 1
                else:
                    coords, _ratio, _th, _tw = encoder_mod.tile_diff(
                        prev, cur, ts, a.change_threshold)
                    prev = cur

                    if coords or pending:
                        # 把待补发的块并进来 —— 保证丢帧后画面能自愈
                        all_coords = set(coords) | pending
                        pending.clear()
                        img = Image.fromarray(cur)
                        atlas, tiles = encoder_mod.build_atlas(img, sorted(all_coords), ts)
                        if atlas is not None:
                            jpeg = encoder_mod.encode_jpeg(atlas, q)
                            packet = protocol.encode_frame_packet(
                                tiles, atlas.width, atlas.height, q, jpeg, keyframe=False)
                            stale = self._outbox.put(packet, tiles)
                            if stale:
                                # 这些块被丢了，下一帧必须补发
                                pending.update((ty, tx) for ty, tx, _, _ in stale)
                            dirty_since_keyframe = True
                            stat_frames += 1
                            stat_bytes += len(packet)

                    # 定期关键帧：只在「上次关键帧之后确实发过更新」时才需要。
                    # 画面一直静止时 Viewer 的图本来就是对的，白发关键帧纯属浪费带宽。
                    if dirty_since_keyframe and (time.time() - last_keyframe) >= a.keyframe_interval:
                        self._force_keyframe.set()

                self.stats["frames_captured"] += 1
            except Exception as e:
                # 采集线程里任何异常都不能让它悄悄死掉
                self.log("[采集] 处理帧出错：%s: %s" % (type(e).__name__, e))

            # 每 5 秒打一次状态
            now = time.time()
            if now - last_stat >= 5.0:
                depth = self._outbox.depth() if self._outbox else 0
                self.log("[状态] 队列 %d/%d  质量 q%d  5秒内 %d 帧 %.0f KB"
                         % (depth, a.send_queue_max, quality.quality,
                            stat_frames, stat_bytes / 1024.0))
                stat_frames = 0
                stat_bytes = 0
                last_stat = now
                # 队列深度是最直接的拥塞信号，比猜 RTT 可靠
                quality.observe(depth)

            # 限帧。注意限的是「处理帧率」，不是「变化帧率」
            spent = time.perf_counter() - t_start
            if spent < frame_interval:
                time.sleep(frame_interval - spent)

    def _push_keyframe(self, cur: np.ndarray, quality: int) -> int:
        """编码并投递一个整屏关键帧，返回包大小。"""
        img = Image.fromarray(cur)
        jpeg = encoder_mod.encode_full_frame(img, quality)
        packet = protocol.encode_frame_packet(
            [], img.width, img.height, quality, jpeg, keyframe=True)
        self._outbox.put(packet, [])
        self.stats["keyframes"] += 1
        return len(packet)

    # ------------------------------------------------------------ 发送 / 接收

    async def _send_loop(self, sess: wssession.Session) -> None:
        """从 Outbox 取包并加密发送。"""
        while True:
            packet, _tiles = await self._outbox.get()
            await sess.send(protocol.MSG_FRAME, packet)
            self.stats["frames_sent"] += 1
            self.stats["bytes_sent"] += len(packet)
            self.stats["dropped"] = self._outbox.dropped

    async def _recv_loop(self, sess: wssession.Session) -> None:
        """处理来自 Viewer 的消息。"""
        while True:
            mtype, payload = await sess.recv_peer_message()
            self._last_viewer_activity = time.time()

            if mtype == protocol.MSG_CTRL:
                msg = protocol.parse_ctrl(payload)
                kind = msg.get("t")
                if kind == protocol.CTRL_KEYFRAME:
                    self._force_keyframe.set()
                    self.log("[控制] Viewer 请求关键帧")
                elif kind == protocol.CTRL_PARAMS:
                    self._apply_params(msg)
                elif kind == protocol.CTRL_PING:
                    await sess.send_ctrl(protocol.CTRL_PONG, ts=msg.get("ts"))
                elif kind == protocol.CTRL_CLIPBOARD:
                    text = protocol.clipboard_text_from(msg)
                    if text and self._clipboard is not None:
                        self._clipboard.set_text(text)
                elif kind == protocol.CTRL_BYE:
                    self.log("[控制] Viewer 主动断开")
                    return
                continue

            if mtype == protocol.MSG_INPUT:
                if not self.cfg.agent.allow_input or self._injector is None:
                    # 【保命措施】只读模式：明确记数并丢弃，绝不静默注入
                    self.stats["input_ignored"] += 1
                    if self.stats["input_ignored"] % 50 == 1:
                        self.log("[只读] 已忽略 %d 个键鼠事件（allow_input=false）"
                                 % self.stats["input_ignored"])
                    continue
                try:
                    ev = protocol.parse_input(payload)
                except protocol.ProtocolError as e:
                    self.log("[注入] 键鼠事件解析失败：%s" % e)
                    continue
                injected = self._injector.handle_event(ev)
                if injected:
                    self.stats["input_injected"] += 1
                else:
                    self.stats["input_blocked"] += 1
                continue

            self.log("[会话] 收到未处理的消息类型 %s" % protocol.msg_name(mtype))

    def _apply_params(self, msg: Dict[str, Any]) -> None:
        """Viewer 请求调整参数。Phase 1 只支持质量；分辨率/帧率留到 Phase 4 自适应。"""
        if "quality" in msg:
            try:
                q = int(msg["quality"])
                self._quality_override = max(self.cfg.agent.jpeg_quality_min,
                                             min(self.cfg.agent.jpeg_quality_max, q))
                self.log("[控制] 质量设为 q%d" % self._quality_override)
            except (TypeError, ValueError):
                pass
        for key in ("max_fps", "target_width"):
            if key in msg:
                self.log("[控制] 参数 %s=%s 暂未支持（Phase 4）" % (key, msg[key]))

    async def _watchdog(self, sess: wssession.Session) -> None:
        """急停响应 + 空闲自动断开。

        【保命措施】两件事：
        1. 急停热键一按下就立刻断开会话（不等任何别的条件）。
        2. 远程无人操作超时后自动松手 —— 避免 Viewer 崩了却还挂着会话，
           更避免键鼠注入卡住本机。
        """
        limit = self.cfg.agent.idle_disconnect_seconds
        while True:
            await asyncio.sleep(0.5)          # 用 0.5 秒小步进，保证急停能立刻生效
            if self._panic.is_set():
                self.log("[保命] 急停已按下，立即断开远程会话")
                await sess.close()
                return
            if not limit or limit <= 0:
                continue
            idle = time.time() - self._last_viewer_activity
            if idle >= limit:
                self.log("[保命] 空闲 %.0f 秒（上限 %s），自动断开并松开按键" % (idle, limit))
                if self._injector is not None:
                    self._injector.release_all()
                await sess.close()
                return
