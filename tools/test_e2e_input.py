#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_e2e_input.py —— Phase 2 键鼠注入链路的端到端测试

⚠️ 安全说明
-----------
本项目**开发机就是被控机**。所以这里始终把 Agent 的注入器设为 **dry-run**：
Viewer 真的发键鼠事件、走真的加密与中继、Agent 真的处理并调用注入器，
但注入器只**记录**应该注入什么，**绝不调用 SendInput**。
因此可以放心运行，不会夺走你的鼠标键盘。

覆盖场景
--------
A. **只读模式**（allow_input=false）：Viewer 发键鼠 -> Agent 必须完全忽略，且不创建注入器
B. **注入模式（dry-run）**：Viewer 发的移动/点击/滚轮/按键，必须被准确还原成
   正确的 INPUT 结构（绝对坐标、按键标志、扫描码）
C. **急停热键**：触发后必须立刻暂停注入、松开所有按键、并**彻底停止 Agent**（含重连）
D. **空闲自动断连**：Viewer 不再发心跳时，Agent 必须自动断开并松开按键

用法
----
    python tools/test_e2e_input.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import numpy as np

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import config as config_mod          # noqa: E402
from common.console import setup_console         # noqa: E402

setup_console()

from agent import clipboard as clipboard_mod
from agent import input as inp_mod             # noqa: E402
from agent.session import AgentSession           # noqa: E402
from viewer import session as viewer_session_mod  # noqa: E402
from viewer.session import ViewerSession         # noqa: E402

try:
    from websockets.asyncio.server import serve
except ImportError:
    print("需要 websockets 库")
    raise

import relay.server as relay                     # noqa: E402

RELAY_PORT = 18545
ROOM = "e2e-input-room"
TOKEN = "e2e-input-token-0123456789abcdef"
PASSWORD = "e2e-input-password-0123456789"
TILE = 64
W, H = 640, 448

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


class FakeCapturer:
    """受控假采集器：只在图案被设置时返回一帧，其余时候返回 None。"""

    name = "fake (测试用)"
    width = W
    height = H

    def __init__(self):
        self._pending = np.zeros((H, W, 3), dtype=np.uint8)
        self._last = None
        self._pending_set = True

    def set_pattern(self, img):
        self._pending = img
        self._pending_set = True

    def grab(self, force=False):
        if self._pending_set:
            self._pending_set = False
            self._last = self._pending
            return self._last
        if force and self._last is not None:
            return self._last
        return None

    def monitor_rect(self):
        # 固定一个已知的显示器区域，让坐标换算可确定性验证
        return (0, 0, 1920, 1080)

    def close(self):
        pass


def build_config(allow_input: bool, idle_seconds: int = 0) -> config_mod.AppConfig:
    cfg = config_mod.AppConfig()
    cfg.common.relay_host = "127.0.0.1"
    cfg.common.relay_ports = [RELAY_PORT]
    cfg.common.room = ROOM
    cfg.common.relay_token = TOKEN
    cfg.common.password = PASSWORD
    cfg.tls.enabled = False
    cfg.agent.capture_backend = "mss"
    cfg.agent.tile_size = TILE
    cfg.agent.max_fps = 10
    cfg.agent.keyframe_interval = 999
    cfg.agent.allow_input = allow_input
    # 关键：演练模式，绝不真的操作键鼠
    cfg.agent.input_dry_run = True
    # 活动检测关掉：测试里假光标不会跟着注入移动，开了会误触发
    cfg.agent.input_activity_guard = False
    cfg.agent.idle_disconnect_seconds = idle_seconds
    # 急停热键用一个不常用的组合，避免和真实系统冲突；测试里不真按键
    cfg.agent.panic_hotkey = "ctrl+alt+shift+f24"
    return cfg


class Scenario:
    """一次「中继 + Agent + Viewer」的完整运行。"""

    def __init__(self, cfg, log_agent=False):
        self.cfg = cfg
        self.agent = AgentSession(cfg, log=(lambda m: print("      [agent] %s" % m))
                                  if log_agent else (lambda m: None))
        self.agent._capturer = FakeCapturer()
        self.received_clipboard = []
        self.viewer = ViewerSession(
            cfg,
            on_frame=lambda a, m: None,
            on_status=lambda s: None,
            on_clipboard=self.received_clipboard.append,
            log=(lambda m: print("      [viewer] %s" % m)) if log_agent else (lambda m: None),
            enable_input=True,
        )
        self.agent_task = None
        self.viewer_task = None

    async def start(self, wait_paired: bool = True, timeout: float = 15.0):
        self.agent_task = asyncio.create_task(self.agent.run(), name="agent")
        self.viewer_task = asyncio.create_task(self.viewer.run(), name="viewer")
        if not wait_paired:
            await asyncio.sleep(1.0)
            return
        # 必须等到**两端真的配对完成**再发事件。用固定 sleep 会偶发失败：
        # 配对没完成时 Viewer 的 _session 还是 None，send_* 会静默丢弃。
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.1)
            if self.viewer._session is not None and self.agent._outbox is not None:
                await asyncio.sleep(0.3)      # 再等一下让首帧/握手彻底稳定
                return
        raise RuntimeError("等待配对超时")

    async def stop(self):
        self.viewer.stop()
        self.agent.stop()
        for t in (self.agent_task, self.viewer_task):
            if t:
                t.cancel()
        await asyncio.gather(*[t for t in (self.agent_task, self.viewer_task) if t],
                             return_exceptions=True)

    def paired(self) -> bool:
        return self.agent._injector is not None or self.viewer.peer is not None


def flags_of(inp_obj):
    return inp_obj.union.mi.dwFlags


def scan_of(inp_obj):
    return inp_obj.union.ki.wScan


def collect_inputs(calls):
    """从注入器记录里取出 INPUT 对象。

    记录里既有 ``(INPUT, ...)``，也有 ``("release_all",)`` 这种标记，
    所以必须过滤，不能直接下标取用。
    """
    out = []
    for c in calls:
        if not isinstance(c, tuple):
            continue
        for x in c:
            if hasattr(x, "union") and hasattr(x, "type"):
                out.append(x)
    return out


def mouse_flags(calls):
    return [flags_of(x) for x in collect_inputs(calls) if x.type == inp_mod.INPUT_MOUSE]


def key_inputs(calls):
    return [x for x in collect_inputs(calls) if x.type == inp_mod.INPUT_KEYBOARD]


# ============================================================ 场景 A

async def scenario_readonly():
    print()
    print("=" * 74)
    print("  场景 A：只读模式（allow_input = false）")
    print("=" * 74)
    cfg = build_config(allow_input=False)
    sc = Scenario(cfg)
    await sc.start()
    try:
        check("Agent 未创建注入器（只读模式）", sc.agent._injector is None)
        # Viewer 真的发键鼠事件
        for i in range(5):
            sc.viewer.send_mouse_move(0.1 * i, 0.2 * i)
        sc.viewer.send_mouse_button(1, True, 0.5, 0.5)
        sc.viewer.send_key(0x1E, True)
        await asyncio.sleep(1.0)

        check("Agent 统计到被忽略的键鼠事件",
              sc.agent.stats["input_ignored"] >= 6,
              "input_ignored=%d" % sc.agent.stats["input_ignored"])
        check("Agent 完全没有任何注入", sc.agent.stats["input_injected"] == 0,
              "input_injected=%d" % sc.agent.stats["input_injected"])
    finally:
        await sc.stop()


# ============================================================ 场景 B

async def scenario_inject_dryrun():
    print()
    print("=" * 74)
    print("  场景 B：注入模式 + 演练（dry-run，不碰真实键鼠）")
    print("=" * 74)
    cfg = build_config(allow_input=True)
    sc = Scenario(cfg)
    await sc.start()
    try:
        inj = sc.agent._injector
        if not check("Agent 已创建注入器", inj is not None):
            return
        check("注入器处于演练模式（不会真的注入）", inj.dry_run is True)
        check("使用假采集器给的显示器区域 (0,0,1920,1080)",
              (inj.mon_left, inj.mon_top, inj.mon_w, inj.mon_h) == (0, 0, 1920, 1080),
              "%s" % ((inj.mon_left, inj.mon_top, inj.mon_w, inj.mon_h),))

        n0 = len(inj.calls)
        sc.viewer.send_mouse_move(0.25, 0.75)
        await asyncio.sleep(0.5)
        new_ms = mouse_flags(inj.calls[n0:])
        moves = [f for f in new_ms if f & inp_mod.MOUSEEVENTF_MOVE]
        if check("鼠标移动事件被注入器接收", len(moves) >= 1,
                 "%d 条（鼠标事件 flags=%s）" % (len(moves), [hex(f) for f in new_ms])):
            # 取回该次移动的绝对坐标
            moved = [x for x in collect_inputs(inj.calls[n0:])
                     if x.type == inp_mod.INPUT_MOUSE
                     and flags_of(x) & inp_mod.MOUSEEVENTF_MOVE]
            ax = moved[-1].union.mi.dx
            ay = moved[-1].union.mi.dy

            # 做**往返校验**，而不是比对写死的期望值：
            # 把绝对坐标反算回虚拟桌面像素，应当落在目标显示器上被请求的那个像素。
            # 注入器用的是真实的虚拟桌面尺寸（本机可能是多屏 4480x1646），
            # 所以只有往返校验才是与机器无关的正确断言 ——
            # 它能同时抓出「显示器偏移算错」「虚拟桌面尺寸用错」「缩放公式写错」。
            back_x = inj.virt_left + ax * (inj.virt_w - 1) / 65535.0
            back_y = inj.virt_top + ay * (inj.virt_h - 1) / 65535.0
            want_x = inj.mon_left + 0.25 * inj.mon_w
            want_y = inj.mon_top + 0.75 * inj.mon_h
            check("鼠标绝对坐标往返正确（0.25/0.75 应落在目标显示器的 480/810 像素）",
                  abs(back_x - want_x) <= 1.5 and abs(back_y - want_y) <= 1.5,
                  "反算得 (%.1f,%.1f)，期望 (%.1f,%.1f)｜绝对坐标 (%d,%d)｜虚拟桌面 %dx%d"
                  % (back_x, back_y, want_x, want_y, ax, ay, inj.virt_w, inj.virt_h))

        n1 = len(inj.calls)
        sc.viewer.send_mouse_button(2, True, 0.5, 0.5)
        await asyncio.sleep(0.5)
        downs = [f for f in mouse_flags(inj.calls[n1:])
                 if f == inp_mod.MOUSEEVENTF_RIGHTDOWN]
        moves_in_btn = [f for f in mouse_flags(inj.calls[n1:])
                        if f & inp_mod.MOUSEEVENTF_MOVE]
        check("右键按下被注入", len(downs) >= 1, "%d 条" % len(downs))
        check("点击前先移动到目标位置", len(moves_in_btn) >= 1, "%d 条" % len(moves_in_btn))

        n2 = len(inj.calls)
        sc.viewer.send_wheel(120, 0.5, 0.5)
        await asyncio.sleep(0.5)
        wheels = [f for f in mouse_flags(inj.calls[n2:])
                  if f == inp_mod.MOUSEEVENTF_WHEEL]
        check("滚轮事件被注入", len(wheels) >= 1, "%d 条" % len(wheels))

        n3 = len(inj.calls)
        sc.viewer.send_key(0x1E, True)          # A 的扫描码
        await asyncio.sleep(0.5)
        keys = key_inputs(inj.calls[n3:])
        if check("按键事件被注入", len(keys) >= 1, "%d 条" % len(keys)):
            k = keys[-1]
            check("按键用扫描码 0x1E 且无 KEYUP",
                  scan_of(k) == 0x1E and not (k.union.ki.dwFlags & inp_mod.KEYEVENTF_KEYUP),
                  "scan=0x%X flags=0x%X" % (scan_of(k), k.union.ki.dwFlags))

        check("Agent 统计注入次数", sc.agent.stats["input_injected"] >= 4,
              "input_injected=%d" % sc.agent.stats["input_injected"])
        check("Agent 没有走「忽略」分支", sc.agent.stats["input_ignored"] == 0,
              "input_ignored=%d" % sc.agent.stats["input_ignored"])
    finally:
        await sc.stop()


# ============================================================ 场景 C

async def scenario_panic():
    print()
    print("=" * 74)
    print("  场景 C：急停热键（直接调用回调，不真的按键）")
    print("=" * 74)
    cfg = build_config(allow_input=True)
    sc = Scenario(cfg)
    await sc.start()
    try:
        inj = sc.agent._injector
        if not check("Agent 已创建注入器", inj is not None):
            return
        sc.viewer.send_mouse_move(0.5, 0.5)
        await asyncio.sleep(0.4)
        check("急停前可以正常注入", inj.enabled is True and inj.stats["mouse_moves"] >= 1)

        # 触发急停（等价于按下热键）
        sc.agent._on_panic()
        check("急停后注入立即被暂停", inj.enabled is False,
              "paused_reason=%s" % inj.paused_reason)
        check("急停后 stop_flag 被设置（不会自动重连）", sc.agent.stop_flag.is_set())
        check("急停时调用了 release_all（松开所有按键）",
              any(isinstance(c, tuple) and "release_all" in c for c in inj.calls),
              "最后一条记录 = %r" % (inj.calls[-1],))

        # 急停后再来键鼠事件：必须完全不注入
        n = inj.stats["mouse_moves"]
        sc.viewer.send_mouse_move(0.7, 0.7)
        await asyncio.sleep(0.5)
        check("急停后不再注入任何事件", inj.stats["mouse_moves"] == n,
              "mouse_moves %d -> %d" % (n, inj.stats["mouse_moves"]))

        # Agent 应当自行退出（不再重连）
        try:
            await asyncio.wait_for(sc.agent_task, timeout=5.0)
            check("Agent 已彻底停止（不是仅断线等重连）", True)
        except asyncio.TimeoutError:
            check("Agent 已彻底停止（不是仅断线等重连）", False, "Agent 仍在运行")
    finally:
        await sc.stop()


# ============================================================ 场景 D

async def scenario_idle_disconnect():
    print()
    print("=" * 74)
    print("  场景 D：空闲自动断连（Viewer 停发心跳）")
    print("=" * 74)
    cfg = build_config(allow_input=True, idle_seconds=2)
    # 把 Viewer 的 ping 间隔调大，模拟「Viewer 还连着但不再说话」
    old_ping = viewer_session_mod.PING_INTERVAL
    viewer_session_mod.PING_INTERVAL = 60.0
    sc = Scenario(cfg)
    try:
        await sc.start()
        inj = sc.agent._injector
        if not check("Agent 已创建注入器", inj is not None):
            return
        sc.viewer.send_mouse_move(0.3, 0.3)
        await asyncio.sleep(0.4)
        check("断连前可以正常注入", inj.stats["mouse_moves"] >= 1)

        print("      等待空闲超时（2 秒）……")
        deadline = time.time() + 8.0
        closed = False
        while time.time() < deadline:
            await asyncio.sleep(0.3)
            if any(isinstance(c, tuple) and "release_all" in c for c in inj.calls):
                closed = True
                break
        check("空闲超时后自动断开并松开按键", closed,
              "release_all 记录 = %s" % any(isinstance(c, tuple) and "release_all" in c
                                          for c in inj.calls))
    finally:
        viewer_session_mod.PING_INTERVAL = old_ping
        await sc.stop()


# ============================================================ 场景 E

async def scenario_clipboard():
    print()
    print("=" * 74)
    print("  场景 E：剪贴板同步（双向，经加密与中继）")
    print("=" * 74)

    # 用假剪贴板后端替换掉真实实现 —— 测试不会读、也不会覆盖你真实的剪贴板。
    # ClipboardWatcher 在构造时读取模块级函数，所以这里替换模块属性即可生效。
    fake = {"agent": "Agent 端初始内容", "writes": 0}

    def fake_read():
        return fake["agent"]

    def fake_write(text):
        fake["agent"] = text
        fake["writes"] += 1
        return True

    orig_read = clipboard_mod.read_clipboard_text
    orig_write = clipboard_mod.write_clipboard_text
    clipboard_mod.read_clipboard_text = fake_read
    clipboard_mod.write_clipboard_text = fake_write

    cfg = build_config(allow_input=True)
    # 让 Agent 的剪贴板轮询快一点，测试才不用等太久
    cfg.agent.clipboard_interval = 0.15
    sc = Scenario(cfg)
    try:
        await sc.start()
        watcher = sc.agent._clipboard
        if not check("Agent 已启动剪贴板同步", watcher is not None):
            return

        # ---- Viewer -> Agent ----
        n_before = fake["writes"]
        sc.viewer.send_clipboard("从家里复制的内容")
        ok = await asyncio.wait_for(_wait_until(lambda: fake["writes"] > n_before), timeout=6)
        check("Viewer -> Agent：内容已写入 Agent 的剪贴板",
              ok and fake["agent"] == "从家里复制的内容",
              "agent 剪贴板 = %r" % fake["agent"])

        # 回环检查：Agent 写入后**不应**再把它同步回 Viewer
        await asyncio.sleep(0.6)
        check("【回环防护】Agent 没有把刚写入的内容又发回 Viewer",
              len(sc.received_clipboard) == 0,
              "Viewer 收到 %d 条：%s" % (len(sc.received_clipboard), sc.received_clipboard[:3]))

        # ---- Agent -> Viewer ----
        fake["agent"] = "公司电脑上复制的代码"
        ok = await asyncio.wait_for(
            _wait_until(lambda: len(sc.received_clipboard) >= 1), timeout=6)
        check("Agent -> Viewer：本机剪贴板变化已同步到 Viewer",
              ok and sc.received_clipboard[-1] == "公司电脑上复制的代码",
              "Viewer 收到：%s" % sc.received_clipboard[:3])

        # ---- 再次往返 ----
        sc.viewer.send_clipboard("再来一次")
        ok = await asyncio.wait_for(
            _wait_until(lambda: fake["agent"] == "再来一次"), timeout=6)
        check("第二轮往返仍正常", ok, "agent 剪贴板 = %r" % fake["agent"])
        await asyncio.sleep(0.6)
        check("第二轮也没有回环",
              all(s != "再来一次" for s in sc.received_clipboard),
              "Viewer 收到：%s" % sc.received_clipboard[:4])

        st = watcher.stats
        print("       Agent 剪贴板统计：发送 %d / 应用 %d / 过大跳过 %d / 写入失败 %d"
              % (st["sent"], st["applied"], st["skipped_too_long"], st["write_failed"]))
    finally:
        clipboard_mod.read_clipboard_text = orig_read
        clipboard_mod.write_clipboard_text = orig_write
        await sc.stop()


async def _wait_until(pred, interval=0.05):
    while not pred():
        await asyncio.sleep(interval)
    return True


# ============================================================ main

async def main() -> int:
    print()
    print("=" * 74)
    print("  ALSPD-DESK  Phase 2  键鼠注入链路  端到端测试")
    print("=" * 74)
    print("  ⚠️  全程 dry-run：Viewer 真发事件、加密与中继都是真的，")
    print("     但 Agent 的注入器只记录不执行 —— **不会动你的键鼠**。")
    print("     剪贴板测试也用假后端，**不读、不覆盖你真实的剪贴板**。")
    print("-" * 74)

    relay.expected_token = TOKEN
    relay.expected_room = ROOM
    relay.LOG.setLevel(40)

    async with serve(relay.handle, "127.0.0.1", RELAY_PORT, max_size=16 * 1024 * 1024,
                     ping_interval=None, compression=None):
        await scenario_readonly()
        await scenario_inject_dryrun()
        await scenario_panic()
        await scenario_idle_disconnect()
        await scenario_clipboard()

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  只读保护、注入准确性、急停、空闲断连都符合预期。")
        print("  注：急停热键的真实按键效果仍需你手动验证一次。")
        print("=" * 74)
        return 0
    print("  ❌ 有失败项：%d/%d 通过" % (passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 74)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
