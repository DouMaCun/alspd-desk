#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_e2e_readonly.py —— Phase 1 只读链路的端到端测试（无界面）

为什么用「假采集器」
--------------------
真实屏幕一直在变，没法做确定性断言。所以这个测试给 Agent 注入一个
**受控的假采集器**，于是整条链路的输出可以逐像素校验：

    假采集器 -> Agent(差分/atlas/加密) -> 中继(盲转) -> Viewer(解密/还原) -> 画布

这样任何一环出错都会被抓到：
* 分块坐标算错 -> 贴图位置错 -> 像素对不上
* atlas 布局错 -> 块内容错位 -> 像素对不上
* 加解密/分帧错 -> 直接报错或数据损坏
* 变化块漏检 -> 画布停留在旧内容 -> 像素对不上

场景（时间轴）
--------------
::

    0.0s  图案 A        -> Agent 发关键帧，Viewer 建立画布
    1.0s  静止（None）   -> 应当**完全不发包**（验证静止零开销）
    2.5s  图案 B        -> 只有少数块变化 -> Agent 发增量更新
    4.0s  校验画布 == 图案 B
    4.5s  图案 C        -> 大面积变化
    6.0s  校验画布 == 图案 C

用法
----
    python tools/test_e2e_readonly.py
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

from agent.session import AgentSession           # noqa: E402
from viewer.session import ViewerSession         # noqa: E402

try:
    from websockets.asyncio.server import serve
except ImportError:
    print("需要 websockets 库")
    raise

import relay.server as relay                     # noqa: E402

RELAY_PORT = 18544
ROOM = "e2e-test-room"
TOKEN = "e2e-test-token-0123456789abcdef"
PASSWORD = "e2e-test-password-0123456789"
TILE = 64
GRID_W, GRID_H = 10, 7                      # 640x448
W, H = GRID_W * TILE, GRID_H * TILE

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


# ============================================================ 假采集器

class FakeCapturer:
    """受控的假采集器：只在「图案被设置」时返回一帧，其余时候返回 None。

    这正好模拟 dxcam 的行为（画面无变化时返回 None）。
    """

    name = "fake (测试用)"
    width = W
    height = H

    def __init__(self):
        self._pending: np.ndarray | None = None
        self._last: np.ndarray | None = None

    def set_pattern(self, img: np.ndarray) -> None:
        self._pending = img

    def grab(self, force: bool = False):
        if self._pending is not None:
            self._last = self._pending
            self._pending = None
            return self._last
        if force and self._last is not None:
            return self._last
        return None

    def close(self) -> None:
        pass


def make_pattern(seed: int, changed_tiles=(), change_color=(255, 0, 0)) -> np.ndarray:
    """生成一张有明确结构的图案（渐变 + 网格线 + 标记），便于发现错位。"""
    rng = np.random.default_rng(seed)
    img = np.zeros((H, W, 3), dtype=np.uint8)
    # 渐变底：位置不同颜色不同 —— 一旦错位，颜色就明显不对
    xs = np.linspace(0, 200, W, dtype=np.uint8)
    ys = np.linspace(0, 200, H, dtype=np.uint8)
    img[..., 0] = xs[None, :]
    img[..., 1] = ys[:, None]
    img[..., 2] = 120
    # 每块加一个独有标记，块与块不会混淆
    for ty in range(GRID_H):
        for tx in range(GRID_W):
            img[ty * TILE + 4: ty * TILE + 16, tx * TILE + 4: tx * TILE + 16] = \
                ((ty * 37) % 256, (tx * 53) % 256, 200)
    # 小幅噪声：避免整张图被 JPEG 压成纯色而掩盖问题。
    # 注意幅度不能大 —— ±20 的均匀噪声在 q60 下会产生约 10 的像素级误差，
    # 那是 JPEG 的正常损失，不是链路问题，会淹没真正的错误信号。
    img = np.clip(img.astype(np.int16) + rng.integers(-6, 7, img.shape), 0, 255).astype(np.uint8)
    # 指定块涂成指定颜色
    for (ty, tx) in changed_tiles:
        img[ty * TILE:(ty + 1) * TILE, tx * TILE:(tx + 1) * TILE] = change_color
    return img


def compare(got: np.ndarray, want: np.ndarray, tile: int = TILE) -> dict:
    """比对收到的画布与期望图案。

    核心是**逐块均值比对**，而不是只看像素级误差：

    * 每个块里都放了一个独有的标记色，所以「块贴到错误位置」会让该块均值严重偏离，
      极其容易发现（这正是 atlas 布局/分块坐标最容易出错的地方）。
    * 而 JPEG 是有损的，像素级误差天然存在；但 4096 个像素求均值会把噪声平均掉，
      所以块均值对 JPEG 损失不敏感。

    这样「真正的错误」和「正常的压缩损失」就被分开了。
    """
    out = {"within": 0.0, "mad": 255.0, "bad": [], "worst": (0.0, -1, -1), "error": None}
    if got is None:
        out["error"] = "画布为空"
        return out
    if got.shape != want.shape:
        out["error"] = "尺寸不符 got=%s want=%s" % (got.shape, want.shape)
        return out

    d = np.abs(got.astype(np.int16) - want.astype(np.int16))
    out["within"] = float((d.max(axis=2) <= 30).mean())
    out["mad"] = float(d.mean())

    th, tw = got.shape[0] // tile, got.shape[1] // tile
    worst = (0.0, -1, -1)
    for ty in range(th):
        for tx in range(tw):
            g = got[ty * tile:(ty + 1) * tile, tx * tile:(tx + 1) * tile].reshape(-1, 3).mean(axis=0)
            w = want[ty * tile:(ty + 1) * tile, tx * tile:(tx + 1) * tile].reshape(-1, 3).mean(axis=0)
            e = float(np.abs(g - w).max())
            if e > worst[0]:
                worst = (e, ty, tx)
            if e > 12.0:
                out["bad"].append((ty, tx, round(e, 1)))
    out["worst"] = worst
    return out


def report(tag: str, res: dict) -> None:
    if res.get("error"):
        print("       %s" % res["error"])
        return
    print("       %s 逐块均值：%d 个块异常（阈值 12）  最差块 (ty=%d,tx=%d) 偏差 %.1f"
          % (tag, len(res["bad"]), res["worst"][1], res["worst"][2], res["worst"][0]))
    print("       像素级：匹配率 %.1f%%  平均误差 %.2f（JPEG 有损，这是正常范围）"
          % (res["within"] * 100, res["mad"]))
    if res["bad"]:
        print("       异常块前 8 个：%s" % res["bad"][:8])


# ============================================================ 主流程

def build_config() -> config_mod.AppConfig:
    cfg = config_mod.AppConfig()
    cfg.common.relay_host = "127.0.0.1"
    cfg.common.relay_ports = [RELAY_PORT]
    cfg.common.room = ROOM
    cfg.common.relay_token = TOKEN
    cfg.common.password = PASSWORD
    cfg.tls.enabled = False              # TLS 通道已在 smoke_test_relay.py 里单独验证
    cfg.agent.capture_backend = "mss"    # 占位，实际会被注入的假采集器替换
    cfg.agent.tile_size = TILE
    cfg.agent.max_fps = 25
    cfg.agent.allow_input = False
    cfg.agent.keyframe_interval = 999    # 测试里关掉周期关键帧，便于精确断言
    cfg.agent.idle_disconnect_seconds = 0
    return cfg


async def real_screen_smoke() -> int:
    """用真实屏幕 + 真实采集器（dxcam）跑一遍。

    这里不做逐像素断言（真实屏幕一直在变），只验证新写的
    ``agent/capture.py`` 与整条链路能配合工作。
    """
    print()
    print("=" * 74)
    print("  ALSPD-DESK  真实屏幕冒烟测试")
    print("=" * 74)

    cfg = build_config()
    # 关键：这里必须用 real/auto，才能真正验证 agent/capture.py 里的 dxcam 路径。
    # build_config() 里的 "mss" 只是给注入假采集器的场景用的占位值。
    cfg.agent.capture_backend = "auto"
    cfg.agent.keyframe_interval = 2.0
    cfg.agent.idle_disconnect_seconds = 0
    relay.expected_token = TOKEN
    relay.expected_room = ROOM
    relay.LOG.setLevel(40)

    captured: list = []
    statuses: list = []
    agent = AgentSession(cfg, log=lambda m: print("    [agent] %s" % m))
    viewer = ViewerSession(
        cfg,
        on_frame=lambda arr, meta: captured.append((time.time(), arr)),
        on_status=lambda st: statuses.append(st),
        log=lambda m: print("    [viewer] %s" % m),
        enable_input=False,
    )

    async with serve(relay.handle, "127.0.0.1", RELAY_PORT, max_size=16 * 1024 * 1024,
                     ping_interval=None, compression=None):
        print("  中继已启动，采集真实屏幕 10 秒……")
        agent_task = asyncio.create_task(agent.run(), name="agent")
        viewer_task = asyncio.create_task(viewer.run(), name="viewer")
        try:
            await asyncio.sleep(10.0)
        finally:
            viewer.stop()
            agent.stop()
            for t in (agent_task, viewer_task):
                t.cancel()
            await asyncio.gather(agent_task, viewer_task, return_exceptions=True)

    print()
    if not check("收到画面", len(captured) > 0, "收到 %d 帧" % len(captured)):
        return summarize()

    arr = captured[-1][1]
    cap_obj = agent._capturer
    ts = cfg.agent.tile_size
    exp_w = (cap_obj.width // ts) * ts
    exp_h = (cap_obj.height // ts) * ts
    check("画布尺寸 == 屏幕对齐后尺寸",
          arr.shape == (exp_h, exp_w, 3),
          "画布 %s，期望 (%d, %d, 3)" % (arr.shape, exp_h, exp_w))
    check("画布不是空白（有实际内容）", float(arr.std()) > 5.0,
          "标准差 %.1f" % float(arr.std()))
    check("收到过关键帧", agent.stats["keyframes"] > 0,
          "关键帧 %d 次" % agent.stats["keyframes"])

    elapsed = max(1e-6, time.time() - agent.stats["started"])
    print()
    print("  采集后端  : %s  %dx%d" % (cap_obj.name, cap_obj.width, cap_obj.height))
    print("  采集 %d 帧 / 发送 %d 帧 / %.2f MB / 关键帧 %d / 丢弃 %d"
          % (agent.stats["frames_captured"], agent.stats["frames_sent"],
             agent.stats["bytes_sent"] / 1048576.0, agent.stats["keyframes"],
             agent.stats["dropped"]))
    print("  平均码率  : %.2f Mbps（%d 秒）"
          % (agent.stats["bytes_sent"] * 8 / elapsed / 1e6, elapsed))
    if statuses:
        st = statuses[-1]
        print("  Viewer 状态: %.1f fps  %.2f Mbps  解码 %.1f ms  RTT %.0f ms"
              % (st["fps"], st["mbps"], st["decode_ms"], st["rtt_ms"]))
    return summarize()


async def main() -> int:
    if "--real" in sys.argv:
        return await real_screen_smoke()

    print()
    print("=" * 74)
    print("  ALSPD-DESK  Phase 1 只读链路  端到端测试")
    print("=" * 74)
    print("  画面 %dx%d（%dx%d 块，块边长 %d）" % (W, H, GRID_W, GRID_H, TILE))
    print("  中继 127.0.0.1:%d   TLS 关闭（TLS 另有专项测试）" % RELAY_PORT)
    print("-" * 74)

    cfg = build_config()
    relay.expected_token = TOKEN
    relay.expected_room = ROOM
    relay.LOG.setLevel(40)               # 中继日志静音，只留测试输出

    cap = FakeCapturer()
    pattern_a = make_pattern(1)
    pattern_b = make_pattern(1, changed_tiles=[(1, 1), (1, 2), (2, 1), (5, 8)],
                             change_color=(255, 0, 0))
    pattern_c = make_pattern(2, changed_tiles=[(ty, tx) for ty in range(GRID_H)
                                               for tx in range(GRID_W)],
                             change_color=(0, 255, 0))

    captured: list = []
    statuses: list = []

    agent = AgentSession(cfg, log=lambda m: None)     # Agent 日志静音
    # 注入假采集器，绕开真实屏幕
    agent._capturer = cap

    viewer = ViewerSession(
        cfg,
        on_frame=lambda arr, meta: captured.append((time.time(), arr)),
        on_status=lambda st: statuses.append(st),
        log=lambda m: None,
        enable_input=False,
    )

    async with serve(relay.handle, "127.0.0.1", RELAY_PORT, max_size=16 * 1024 * 1024,
                     ping_interval=None, compression=None):
        print("  中继已启动")

        cap.set_pattern(pattern_a)
        agent_task = asyncio.create_task(agent.run(), name="agent")
        viewer_task = asyncio.create_task(viewer.run(), name="viewer")

        try:
            await asyncio.sleep(1.0)
            print("[1.0s] 图案 A 已投递，等待关键帧与画布建立")
            if not check("Viewer 收到画面", len(captured) > 0,
                         "收到 %d 帧" % len(captured)):
                return summarize()
            canvas_a = captured[-1][1]
            check("画布尺寸与屏幕一致", canvas_a.shape == (H, W, 3),
                  "画布 %s" % (canvas_a.shape,))
            res = compare(canvas_a, pattern_a)
            report("图案A:", res)
            check("关键帧内容与图案 A 一致（逐块位置全对）",
                  not res["error"] and not res["bad"] and res["within"] >= 0.95,
                  res["error"] or "异常块 %d 个，匹配率 %.1f%%" % (len(res["bad"]), res["within"] * 100))

            # ---- 静止期：应当完全不发包 ----
            print()
            print("[1.0-2.5s] 静止（采集器返回 None）")
            await asyncio.sleep(0.6)          # 先让 Viewer 的关键帧请求落地
            frames_before = agent.stats["frames_sent"]
            kf_before = agent.stats["keyframes"]
            n_before = len(captured)
            await asyncio.sleep(1.5)
            frames_during = agent.stats["frames_sent"] - frames_before
            check("静止期间不产生任何报文（零开销）", frames_during == 0,
                  "期间发送 %d 帧" % frames_during)
            check("静止期间 Viewer 没有收到新帧", len(captured) == n_before,
                  "新增 %d 帧" % (len(captured) - n_before))

            # ---- 增量更新 ----
            # 注意：Viewer 一连上就会主动请求一次关键帧，所以此时关键帧数可能已是 2。
            # 这里要以「当前值」为基准来断言，而不是写死 1。
            print()
            print("[2.5s] 图案 B 已投递（4 个块变化），等待增量更新")
            kf_before = agent.stats["keyframes"]
            sent_before = agent.stats["frames_sent"]
            cap.set_pattern(pattern_b)
            await asyncio.sleep(1.2)
            canvas_b = captured[-1][1]
            res = compare(canvas_b, pattern_b)
            report("图案B:", res)
            check("增量更新后画布与图案 B 一致（逐块位置全对）",
                  not res["error"] and not res["bad"] and res["within"] >= 0.95,
                  res["error"] or "异常块 %d 个，匹配率 %.1f%%" % (len(res["bad"]), res["within"] * 100))
            check("增量更新没有触发新的关键帧（只发了变化块）",
                  agent.stats["keyframes"] == kf_before,
                  "关键帧 %d -> %d" % (kf_before, agent.stats["keyframes"]))
            check("增量更新只发了少量帧（未重发整屏）",
                  agent.stats["frames_sent"] - sent_before <= 2,
                  "发送 %d 帧" % (agent.stats["frames_sent"] - sent_before))

            # ---- 大面积变化 ----
            print()
            print("[4.0s] 图案 C 已投递（全部块变化）")
            cap.set_pattern(pattern_c)
            await asyncio.sleep(1.2)
            canvas_c = captured[-1][1]
            res = compare(canvas_c, pattern_c)
            report("图案C:", res)
            check("大面积变化后画布与图案 C 一致（逐块位置全对）",
                  not res["error"] and not res["bad"] and res["within"] >= 0.95,
                  res["error"] or "异常块 %d 个，匹配率 %.1f%%" % (len(res["bad"]), res["within"] * 100))

            # ---- 安全：只读模式下键鼠必须被忽略 ----
            print()
            print("[5.2s] 只读模式安全性检查")
            check("Agent 处于只读模式", cfg.agent.allow_input is False)
            check("Viewer 未启用键鼠发送", viewer.enable_input is False)
            check("Agent 未注入任何键鼠事件",
                  agent.stats["input_ignored"] == 0 and agent.stats["frames_sent"] > 0,
                  "忽略计数 %d" % agent.stats["input_ignored"])

            # ---- 统计 ----
            print()
            total_bytes = agent.stats["bytes_sent"]
            print("  Agent: 采集 %d 帧 / 发送 %d 帧 / %.1f KB / 关键帧 %d / 丢弃 %d"
                  % (agent.stats["frames_captured"], agent.stats["frames_sent"],
                     total_bytes / 1024.0, agent.stats["keyframes"], agent.stats["dropped"]))
            n_static = agent.stats["frames_sent"]
            print("  Viewer: 收到 %d 帧，最后一次状态：%s"
                  % (len(captured), statuses[-1] if statuses else "（无）"))

        finally:
            viewer.stop()
            agent.stop()
            for t in (agent_task, viewer_task):
                t.cancel()
            await asyncio.gather(agent_task, viewer_task, return_exceptions=True)

    return summarize()


def summarize() -> int:
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total and total > 0:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  Phase 1 只读链路（采集→差分→atlas→加密→中继→解密→还原）端到端正确。")
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
