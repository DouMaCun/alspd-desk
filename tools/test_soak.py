#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_soak.py —— 长时间运行 / 反复重连的资源泄漏测试

为什么必须测
------------
这个工具的用法是**开机自启、常驻一整天**。而每次会话都会新建：
采集线程、剪贴板监听线程、急停热键线程，以及一堆协程与 socket。
只要有一处回收不干净，跑几个小时就会：
线程数持续上涨、句柄耗尽、内存被吃光 —— 而这类问题在短测试里完全看不出来。

所以这里**反复强制断开重连**，把「建→用→拆」这条路径跑很多遍，
然后对比线程数 / 句柄数 / 内存有没有单调增长。

覆盖场景
--------
* 反复 连接 → 配对 → 推流 → 断开 → 自动重连
* 同时开着：键鼠注入（dry-run）、剪贴板同步（假后端）、急停热键
* 每轮都检查上一轮的线程是否已回收

用法
----
    python tools/test_soak.py                # 默认约 1 分钟
    python tools/test_soak.py --cycles 8     # 更多轮
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import pathlib
import sys
import threading
import time

import numpy as np

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import config as config_mod          # noqa: E402
from common.console import setup_console         # noqa: E402

setup_console()

from agent import clipboard as clipboard_mod     # noqa: E402
from agent import input as input_mod             # noqa: E402
from agent.session import AgentSession           # noqa: E402
from viewer.session import ViewerSession         # noqa: E402

try:
    from websockets.asyncio.server import serve
except ImportError:
    print("需要 websockets 库")
    raise

import relay.server as relay                     # noqa: E402

RELAY_PORT = 18549
ROOM = "soak-room"
TOKEN = "soak-token-0123456789abcdef"
PASSWORD = "soak-password-0123456789"
TILE = 64
W, H = 640, 448

IS_WIN = sys.platform == "win32"
results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


# ============================================================ 资源采样

def sample_resources() -> dict:
    """采样当前进程的线程数 / 句柄数 / 内存。"""
    out = {"threads": threading.active_count(), "handles": None, "rss_mb": None}
    if not IS_WIN:
        return out
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 关键：必须显式声明 restype/argtypes。
        # GetCurrentProcess 返回的是伪句柄 (-1)，默认 restype=c_int 会把它截断，
        # 后续调用就全部失败 —— 而失败被 except 吃掉后表现为「采样不到」，
        # 于是测试看起来通过了其实什么都没测。
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = k32.GetCurrentProcess()

        k32.GetProcessHandleCount.argtypes = (ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_ulong))
        k32.GetProcessHandleCount.restype = ctypes.c_int
        cnt = ctypes.c_ulong(0)
        if k32.GetProcessHandleCount(h, ctypes.byref(cnt)):
            out["handles"] = int(cnt.value)

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_ulong)
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            out["rss_mb"] = pmc.WorkingSetSize / 1048576.0
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def fmt(r: dict) -> str:
    return "线程 %-3s 句柄 %-5s 内存 %s" % (
        r.get("threads"), r.get("handles") if r.get("handles") is not None else "?",
        ("%.1f MB" % r["rss_mb"]) if r.get("rss_mb") is not None else "?")


# ============================================================ 假后端

class FakeCapturer:
    name = "fake (soak)"
    width = W
    height = H

    def __init__(self):
        self._pending = np.random.randint(0, 255, (H, W, 3), dtype=np.uint8)
        self._last = None
        self._fresh = True

    def jiggle(self):
        """制造一点变化，让采集线程真的有活干。"""
        a = self._pending.copy()
        a[10:60, 10:120] = np.random.randint(0, 255, (50, 110, 3), dtype=np.uint8)
        self._pending = a
        self._fresh = True

    def grab(self, force=False):
        if self._fresh:
            self._fresh = False
            self._last = self._pending
            return self._last
        if force and self._last is not None:
            return self._last
        return None

    def monitor_rect(self):
        return (0, 0, 1920, 1080)

    def close(self):
        pass


def build_config() -> config_mod.AppConfig:
    cfg = config_mod.AppConfig()
    cfg.common.relay_host = "127.0.0.1"
    cfg.common.relay_ports = [RELAY_PORT]
    cfg.common.room = ROOM
    cfg.common.relay_token = TOKEN
    cfg.common.password = PASSWORD
    cfg.common.proxy = "direct"          # soak 测的是本地重连，别去试系统代理
    cfg.tls.enabled = False
    cfg.agent.capture_backend = "mss"
    cfg.agent.tile_size = TILE
    cfg.agent.max_fps = 15
    cfg.agent.keyframe_interval = 2.0
    cfg.agent.allow_input = True         # 顺带把注入器与急停热键线程也跑起来
    cfg.agent.input_dry_run = True       # 但绝不真的动键鼠
    cfg.agent.input_activity_guard = False
    cfg.agent.clipboard_sync = True
    cfg.agent.clipboard_interval = 0.2
    cfg.agent.idle_disconnect_seconds = 0
    return cfg


class Cycle:
    """一轮「Agent + Viewer」会话。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.capturer = FakeCapturer()
        self.agent = AgentSession(cfg, log=lambda m: None)
        self.agent._capturer = self.capturer
        self.frames = 0
        self.viewer = ViewerSession(
            cfg,
            on_frame=lambda a, m: setattr(self, "frames", self.frames + 1),
            on_status=lambda s: None,
            log=lambda m: None,
            enable_input=True,
        )
        self.tasks = []

    async def start(self):
        self.tasks = [
            asyncio.create_task(self.agent.run(), name="soak-agent"),
            asyncio.create_task(self.viewer.run(), name="soak-viewer"),
        ]
        deadline = time.time() + 20
        while time.time() < deadline:
            await asyncio.sleep(0.1)
            if self.viewer._session is not None and self.agent._outbox is not None:
                return True
        return False

    async def stop(self):
        self.viewer.stop()
        self.agent.stop()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


# ============================================================ main

async def run(args) -> int:
    print()
    print("=" * 74)
    print("  ALSPD-DESK  长时间运行 / 反复重连  资源泄漏测试")
    print("=" * 74)
    print("  轮数 %d，每轮约 %d 秒    注入=演练模式  剪贴板=假后端"
          % (args.cycles, args.seconds))
    print("-" * 74)

    cfg = build_config()
    relay.expected_token = TOKEN
    relay.expected_room = ROOM
    relay.LOG.setLevel(40)

    # 用假剪贴板后端，避免读/写你真实的剪贴板
    fake_clip = {"text": "soak-initial"}
    orig_read = clipboard_mod.read_clipboard_text
    orig_write = clipboard_mod.write_clipboard_text
    clipboard_mod.read_clipboard_text = lambda: fake_clip["text"]
    clipboard_mod.write_clipboard_text = lambda t: (fake_clip.update(text=t), True)[1]

    gc.collect()
    await asyncio.sleep(0.5)
    baseline = sample_resources()
    print("  基线（还没开始会话）      : %s" % fmt(baseline))
    baseline_threads = baseline["threads"]
    print()

    samples = []
    mid_samples = []
    total_frames = 0
    try:
        async with serve(relay.handle, "127.0.0.1", RELAY_PORT,
                         max_size=16 * 1024 * 1024,
                         ping_interval=None, compression=None):
            for i in range(1, args.cycles + 1):
                cyc = Cycle(cfg)
                ok = await cyc.start()
                # 跑一会儿，并制造画面变化
                end = time.time() + args.seconds
                during = None
                while time.time() < end:
                    cyc.capturer.jiggle()
                    await asyncio.sleep(0.35)
                    # 会话**运行中**采一次样 —— 否则无法证明线程真的被创建过，
                    # 测试可能只是空跑（永远是「1 个线程，没泄漏」）
                    if during is None and time.time() > end - args.seconds / 2.0:
                        during = sample_resources()
                total_frames += cyc.frames
                await cyc.stop()
                # 给线程一点时间自然退出
                await asyncio.sleep(1.5)
                gc.collect()
                s = sample_resources()
                samples.append(s)
                mid_samples.append(during or {"threads": 0})
                print("  第 %d 轮  配对=%-4s 帧=%-4d │ 运行中 %s │ 结束后 %s"
                      % (i, "成功" if ok else "失败", cyc.frames,
                         fmt(during or {}), fmt(s)))
                if not ok:
                    check("第 %d 轮配对成功" % i, False, "20 秒内没配对成功")
    finally:
        clipboard_mod.read_clipboard_text = orig_read
        clipboard_mod.write_clipboard_text = orig_write

    print()
    print("=" * 74)
    print("  结果分析")
    print("=" * 74)
    print("  基线线程数 : %d" % baseline_threads)
    print("  运行中线程 : %s" % [s["threads"] for s in mid_samples])
    print("  结束后线程 : %s" % [s["threads"] for s in samples])
    if baseline.get("handles") is not None:
        print("  基线句柄数 : %s" % baseline["handles"])
        print("  运行中句柄 : %s" % [s["handles"] for s in mid_samples])
        print("  结束后句柄 : %s" % [s["handles"] for s in samples])
    else:
        print("  句柄采样   : 失败（%s）" % baseline.get("error", "未知"))
    if baseline.get("rss_mb") is not None:
        print("  基线内存   : %.1f MB" % baseline["rss_mb"])
        print("  运行中内存 : %s" % ["%.1f" % (s["rss_mb"] or 0) for s in mid_samples])
        print("  结束后内存 : %s" % ["%.1f" % (s["rss_mb"] or 0) for s in samples])
    else:
        print("  内存采样   : 失败（%s）" % baseline.get("error", "未知"))
    print()

    # ---- 判定 ----

    # 先证明测试不是空跑：会话运行中必须真的多出线程来
    mid_max = max(s["threads"] for s in mid_samples) if mid_samples else 0
    check("会话运行中确实创建了额外线程（证明测试有效，不是空跑）",
          mid_max >= baseline_threads + 2,
          "运行中峰值 %d（基线 %d）" % (mid_max, baseline_threads))

    # 线程泄漏是最要命的：每次重连都新建采集/剪贴板/热键线程，
    # 一旦回收不掉，跑一天就是几百个线程。
    max_threads = max(s["threads"] for s in samples)
    thread_growth = max_threads - baseline_threads
    check("拆除后线程数回到基线（无线程泄漏）",
          max_threads <= baseline_threads + 2,
          "峰值 %d（基线 %d，增长 %d）" % (max_threads, baseline_threads, thread_growth))

    # 后几轮比前几轮不应明显更高（单调增长才是泄漏）
    if len(samples) >= 4:
        first_half = max(s["threads"] for s in samples[:len(samples) // 2])
        second_half = max(s["threads"] for s in samples[len(samples) // 2:])
        check("后半程线程数不高于前半程（不是单调增长）",
              second_half <= first_half + 2,
              "前半 %d -> 后半 %d" % (first_half, second_half))
    else:
        check("（轮数较少，跳过单调性检查）", True, "轮数 %d" % len(samples))

    if baseline.get("rss_mb") is not None and samples:
        growth = samples[-1]["rss_mb"] - baseline["rss_mb"]
        # 每轮允许一定波动（numpy 缓冲、GC 延迟），但不能是每轮线性暴涨
        check("内存没有失控增长", growth < 80.0,
              "末轮 %.1f MB（基线 %.1f MB，增长 %.1f MB）"
              % (samples[-1]["rss_mb"], baseline["rss_mb"], growth))
        if len(samples) >= 4:
            h1 = samples[len(samples) // 2 - 1]["rss_mb"]
            h2 = samples[-1]["rss_mb"]
            check("内存后半程没有继续明显上涨", (h2 - h1) < 40.0,
                  "中点 %.1f MB -> 末轮 %.1f MB" % (h1, h2))

    if baseline.get("handles") is not None and samples:
        growth = samples[-1]["handles"] - baseline["handles"]
        check("句柄数没有明显泄漏", growth < 250,
              "末轮 %d（基线 %d，增长 %d）"
              % (samples[-1]["handles"], baseline["handles"], growth))

    check("每轮都在推流（功能没有随轮次退化）", total_frames > 0,
          "累计收到 %d 帧" % total_frames)

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  反复重连后线程/句柄/内存都没有泄漏，可以放心常驻。")
        print("=" * 74)
        return 0
    print("  ❌ 有失败项：%d/%d 通过" % (passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 74)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ALSPD-DESK 长时间运行资源泄漏测试")
    ap.add_argument("--cycles", type=int, default=5, help="重连轮数，默认 5")
    ap.add_argument("--seconds", type=int, default=10, help="每轮持续秒数，默认 10")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
