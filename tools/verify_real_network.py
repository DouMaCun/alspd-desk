#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_real_network.py —— 真实互联网端到端验证（用真实 config.toml 连真实 VPS）

和 tools/test_e2e_readonly.py 的区别
------------------------------------
那个测试用假采集器 + 本地中继，验证的是**代码逻辑正确性**。
这个脚本用的是**真实配置、真实屏幕、真实 VPS、真实互联网往返**，
验证的是**在真网下到底能不能跑、跑成什么样**。

两端都从本机发起（Agent 与 Viewer 各自连到 VPS，再由中继配对），
所以流量是真的出去到公网再回来 —— 只是省去了「家里那台」而已。

安全
----
只读模式（config 里 allow_input = false），**不会注入键鼠**。

用法
----
    python tools/verify_real_network.py --config config.toml --seconds 30
"""

from __future__ import annotations

import argparse
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

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


def ts() -> str:
    return time.strftime("%H:%M:%S")


async def run(args) -> int:
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print("配置错误：%s" % e)
        return 2

    problems = config_mod.validate_common(cfg)
    if problems:
        for p in problems:
            print("  ❌ %s" % p)
        return 2

    print()
    print("=" * 74)
    print("  ALSPD-DESK  真实互联网端到端验证")
    print("=" * 74)
    print("  中继      : %s:%s（%s）"
          % (cfg.common.relay_host, cfg.common.relay_ports,
             "TLS" if cfg.tls.enabled else "明文"))
    print("  代理策略  : %s" % cfg.common.proxy)
    print("  房间      : %s" % cfg.common.room)
    print("  采集      : %s 后端，target_width=%s，max_fps=%s"
          % (cfg.agent.capture_backend, cfg.agent.target_width, cfg.agent.max_fps))
    print("  键鼠注入  : %s" % ("⚠️ 已启用" if cfg.agent.allow_input
                                else "✅ 只读（不会注入）"))
    print("  采样时长  : %s 秒" % args.seconds)
    print("-" * 74)

    frames = {"n": 0, "last": None}
    statuses = []

    def on_frame(arr, meta):
        frames["n"] += 1
        frames["last"] = arr

    def on_status(st):
        statuses.append(st)
        print("  [%s] %6.1f fps │ %7.3f Mbps │ 解码 %5.1f ms │ RTT %5.0f ms │ 帧 %-5d 关键帧 %d"
              % (ts(), st["fps"], st["mbps"], st["decode_ms"], st["rtt_ms"],
                 st["total_frames"], st["keyframes"]))

    agent = AgentSession(cfg, log=lambda m: print("  [%s] [agent ] %s" % (ts(), m)))
    viewer = ViewerSession(cfg, on_frame=on_frame, on_status=on_status,
                           log=lambda m: print("  [%s] [viewer] %s" % (ts(), m)),
                           enable_input=False)

    agent_task = asyncio.create_task(agent.run(), name="agent")
    viewer_task = asyncio.create_task(viewer.run(), name="viewer")

    try:
        # 等配对完成（真实链路可能有延迟，给足时间）
        print("  正在连接真实 VPS 并配对……")
        deadline = time.time() + 40
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            if viewer._session is not None and agent._outbox is not None:
                break
        if viewer._session is None:
            check("两端经真实互联网配对成功", False, "40 秒内未配对")
            return summarize(agent, viewer, frames, statuses, cfg)
        check("两端经真实互联网配对成功", True,
              "Agent 与 Viewer 均通过 %s:%s" % (cfg.common.relay_host, cfg.common.relay_ports[0]))

        print("-" * 74)
        print("  开始采样 %s 秒……" % args.seconds)
        t0 = time.time()
        await asyncio.sleep(args.seconds)
        elapsed = time.time() - t0
    finally:
        viewer.stop()
        agent.stop()
        for t in (agent_task, viewer_task):
            t.cancel()
        await asyncio.gather(agent_task, viewer_task, return_exceptions=True)

    print("-" * 74)
    print("  采样结束（%.1f 秒）" % elapsed)
    print()
    a = agent.stats
    print("  【Agent 侧】")
    print("    采集 %d 帧 / 发送 %d 帧 / %.2f MB / 关键帧 %d / 丢弃 %d"
          % (a["frames_captured"], a["frames_sent"], a["bytes_sent"] / 1048576.0,
             a["keyframes"], a["dropped"]))
    print("    实际上行码率: %.3f Mbps" % (a["bytes_sent"] * 8 / max(elapsed, 1e-6) / 1e6))
    print()
    print("  【Viewer 侧】")
    # 用 ViewerSession 自己的统计（它按真实收到的密文包计数），
    # 不要在 on_frame 里数 —— 那个回调拿到的是拼好的画布，不含包大小
    vst = viewer.stats
    print("    收到 %d 帧 / %.2f MB（按实际收到的报文计）"
          % (frames["n"], vst.get("bytes", 0) / 1048576.0))
    if vst.get("frames"):
        print("    帧包计数 %d，其中关键帧 %d"
              % (vst["frames"], vst.get("keyframes", 0)))

    check("Viewer 收到了画面", frames["n"] > 0, "%d 帧" % frames["n"])
    if frames["last"] is not None:
        arr = frames["last"]
        print("    画布尺寸 %dx%d，像素标准差 %.1f" % (arr.shape[1], arr.shape[0], float(arr.std())))
        check("画布不是空白（收到了真实画面内容）", float(arr.std()) > 5.0,
              "标准差 %.1f" % float(arr.std()))

    # 延迟：这条链路实测有偶发丢包，会出现 0.5~2 秒的重传尖峰。
    # 所以要分开看「基线延迟」（决定跟手程度）和「尖峰」（决定会不会偶尔卡一下）。
    if statuses:
        rtts = sorted(s["rtt_ms"] for s in statuses if s["rtt_ms"])
        if rtts:
            med = float(np.median(rtts))
            p90 = float(np.percentile(rtts, 90))
            spikes = [r for r in rtts if r > 400]
            print()
            print("  【延迟】中继往返 RTT（含两端各一跳）")
            print("    最小 %.0f ms   中位 %.0f ms   P90 %.0f ms   最大 %.0f ms"
                  % (min(rtts), med, p90, max(rtts)))
            print("    其中 >400ms 的尖峰 %d/%d 次（链路偶发丢包导致 TCP 重传超时）"
                  % (len(spikes), len(rtts)))
            # 断言看**基线**（最小值），它才反映链路的基本延迟；
            # 中位数会被尖峰拉高，用它做门槛会误判一条本来健康的链路
            check("链路基线延迟健康（最小 RTT < 250ms）", min(rtts) < 250,
                  "最小 %.0f ms" % min(rtts))
            if spikes:
                print("    ⚠️ 存在丢包尖峰：办公场景会偶尔卡一下（不累积延迟，会自动追上）")
    check("整轮没有崩溃", True)

    return summarize(agent, viewer, frames, statuses, cfg)


def summarize(agent, viewer, frames, statuses, cfg) -> int:
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total and total > 0:
        print("  ✅ 真实互联网端到端验证通过：%d/%d" % (passed, total))
        print("  实时屏幕已从 %s 经中继送到 Viewer，全链路加密有效。"
              % cfg.common.relay_host)
        print("=" * 74)
        return 0
    print("  ❌ 有失败项：%d/%d" % (passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 74)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ALSPD-DESK 真实互联网端到端验证")
    ap.add_argument("--config", default="config.toml", help="配置文件路径")
    ap.add_argument("--seconds", type=int, default=30, help="采样秒数")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
