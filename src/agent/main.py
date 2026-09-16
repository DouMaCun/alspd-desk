#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent/main.py —— 受控端入口（运行在公司电脑上）

职责
----
主动外连 VPS 中继，把公司电脑的画面加密上传给家里的 Viewer。
**不开放任何入站端口，不需要改防火墙，不需要管理员权限。**

用法
----
    # 用 config.toml（默认会自动查找）
    python src/agent/main.py

    # 指定配置 / 临时开只读模式
    python src/agent/main.py --config config.toml --readonly

    # 只校验配置，不启动
    python src/agent/main.py --check

安全提示
--------
本程序会抓取屏幕内容。首次测试请保持 ``allow_input = false``（只读模式），
确认画面链路稳定后再开启键鼠注入。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys
import time

# 让 `python src/agent/main.py` 能直接 import common
_SRC = pathlib.Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import config as config_mod          # noqa: E402
from common.console import setup_console         # noqa: E402
from common.wssession import HandshakeError      # noqa: E402

setup_console()

from agent.session import AgentSession           # noqa: E402


def build_logger(cfg: config_mod.AppConfig) -> logging.Logger:
    logger = logging.getLogger("agent")
    level = getattr(logging, (cfg.logging.level or "INFO").upper(), logging.INFO)
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if cfg.logging.file:
        try:
            fh = logging.FileHandler(cfg.logging.file, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s"))
            logger.addHandler(fh)
        except OSError as e:
            logger.warning("日志文件打开失败（%s），仅输出到控制台", e)
    return logger


def run_selftest(cfg: config_mod.AppConfig) -> int:
    """bundle 自检：验证打包出来的 exe 在这台机器上是否真的能用。

    打包（PyInstaller）最容易出问题的地方就是「某个模块没被打进去」，
    而这类问题只在运行时才暴露。自检把这些检查提前，且**不连接网络、不注入键鼠**。
    """
    from agent import capture as capture_mod
    from agent import input as input_mod
    from common.wssession import format_fp  # noqa: F401  (仅为触发导入检查)

    problems = 0
    print("=" * 72)
    print("  Agent 自检（不联网、不注入键鼠）")
    print("=" * 72)

    # ---- 1. 依赖模块 ----
    print("[1] 依赖模块")
    for mod_name, label in (("numpy", "numpy"), ("PIL", "pillow"),
                            ("websockets", "websockets"), ("cryptography", "cryptography"),
                            ("dxcam", "dxcam（DXGI 采集）")):
        try:
            mod = __import__(mod_name)
            ver = getattr(mod, "__version__", "")
            print("    ✅ %-22s %s" % (label, ver))
        except ImportError as e:
            print("    %s %-22s 缺失：%s" % ("❌" if mod_name != "dxcam" else "⚠️ ", label, e))
            if mod_name != "dxcam":
                problems += 1
    try:
        import win32api  # noqa: F401
        print("    ✅ %-22s %s" % ("pywin32", ""))
    except ImportError as e:
        print("    ❌ %-22s 缺失：%s（键鼠注入需要它）" % ("pywin32", e))
        problems += 1

    # ---- 2. 采集后端 ----
    print()
    print("[2] 显示器与屏幕采集")
    print("    枚举到的显示器：")
    for line in capture_mod.describe_monitors().splitlines():
        print(line)
    cap = None
    try:
        cap = capture_mod.make_capturer(cfg.agent.capture_backend, cfg.agent.output, print)
        frame = cap.grab(force=True)
        if frame is None:
            print("    ❌ 抓帧返回 None（画面可能完全静止且无缓存帧）")
            problems += 1
        else:
            print("    ✅ 抓到一帧 RGB %s，dtype=%s" % (frame.shape, frame.dtype))
        rect = cap.monitor_rect()
        if rect:
            print("    ✅ 显示器区域 left=%d top=%d %dx%d（多显示器偏移已校正）" % rect)
        else:
            print("    ⚠️  无法确定显示器偏移 —— 键鼠注入在多显示器下可能偏位")
            print("        多半是多块显示器分辨率完全相同、无法区分。")
            print("        可先把 output 留空（采集主屏）再开注入。")
    except Exception as e:
        print("    ❌ 采集初始化失败：%s: %s" % (type(e).__name__, e))
        problems += 1
    finally:
        if cap is not None:
            cap.close()

    # ---- 3. 注入器（演练模式，绝不动真键鼠）----
    print()
    print("[3] 键鼠注入（演练模式，不会操作键鼠）")
    try:
        inj = input_mod.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True)
        inj.inject_mouse_move(0.5, 0.5)
        inj.inject_key(0x1E, True)
        inj.release_all()
        print("    ✅ 注入器构建与调用正常（dry-run，发送 %d 组）" % len(inj.calls))
        print("       虚拟桌面 %dx%d，绝对坐标用例 -> %s"
              % (inj.virt_w, inj.virt_h, inj._abs_coords(0.5, 0.5)))
    except Exception as e:
        print("    ❌ 注入器失败：%s: %s" % (type(e).__name__, e))
        problems += 1

    # ---- 4. 急停热键解析 ----
    print()
    print("[4] 急停热键")
    try:
        mods, vk = input_mod.parse_hotkey(cfg.agent.panic_hotkey)
        print("    ✅ %s -> 修饰键 0x%X，主键 0x%X" % (cfg.agent.panic_hotkey, mods, vk))
        print("       （真实注册与按键效果需启动 Agent 后手动验一次）")
    except ValueError as e:
        print("    ❌ 热键配置非法：%s" % e)
        problems += 1

    # ---- 5. 剪贴板 ----
    print()
    print("[5] 剪贴板同步")
    try:
        from agent import clipboard as clipboard_mod
        import win32clipboard  # noqa: F401
        import win32con        # noqa: F401
        # 只读一次，**不写入** —— 自检不应该动你的剪贴板内容
        text = clipboard_mod.read_clipboard_text()
        if text is None:
            print("    ⚠️  读到一个空的/非文本剪贴板（这不算错误）")
            print("       win32clipboard 已可用，同步功能应该正常")
        else:
            print("    ✅ win32clipboard 可用，读到剪贴板文本（%d 字符，未回显内容）"
                  % len(text))
        print("       注：自检只读不写，你的剪贴板内容没有被改动")
    except ImportError as e:
        print("    ❌ win32clipboard 缺失：%s" % e)
        print("       剪贴板同步会失效（其他功能不受影响），请设为 clipboard_sync = false")
        problems += 1
    except Exception as e:
        print("    ⚠️  剪贴板自检异常（不影响其他功能）：%s: %s" % (type(e).__name__, e))

    # ---- 6. 代理支持 ----
    print()
    print("[6] 代理支持")
    try:
        from common import proxy as proxy_mod
        cands = proxy_mod.detect_all()
        if cands:
            print("    ✅ 探测到 %d 个候选代理：" % len(cands))
            for s in cands[:4]:
                print("       %s" % s.describe())
        else:
            print("    未探测到代理配置（直连环境，正常）")
        # SOCKS 走 python-socks，而 websockets 是**动态**导入它的，
        # 打包时容易漏掉（与 dxcam 那次同一类问题），所以这里显式验证
        try:
            import python_socks  # noqa: F401
            print("    ✅ python_socks 可用（SOCKS 代理可用）")
        except ImportError:
            print("    ⚠️  python_socks 缺失 —— 只能走 HTTP CONNECT 代理，SOCKS 不可用")
            print("        若是打包版，说明构建时漏了 --collect-all python_socks")
    except Exception as e:
        print("    ⚠️  代理自检异常：%s: %s" % (type(e).__name__, e))

    # ---- 7. 配置 ----
    print()
    print("[7] 配置")
    probs = config_mod.validate_common(cfg)
    if probs:
        for p in probs:
            print("    ❌ %s" % p)
            problems += 1
    else:
        print("    ✅ relay_host / room / relay_token / password 均已填写")
    print("    配置文件    : %s" % (cfg.path or "(未找到)"))
    print("    只读模式    : %s" % ("是（不注入键鼠）" if not cfg.agent.allow_input else "⚠️ 否"))
    print("    演练模式    : %s" % ("开启（不真的注入）" if cfg.agent.input_dry_run else "关闭"))
    print("    剪贴板同步  : %s" % ("开启（纯文本，双向）" if cfg.agent.clipboard_sync else "已禁用"))
    print("    代理策略    : %s" % (cfg.common.proxy or "auto"))

    print()
    print("=" * 72)
    if problems == 0:
        print("  ✅ 自检通过（%d 个问题）" % problems)
        print("=" * 72)
        return 0
    print("  ❌ 自检发现 %d 个问题，见上文" % problems)
    print("=" * 72)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ALSPD-DESK 受控端（Agent）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认自动查找 config.toml）")
    ap.add_argument("--readonly", action="store_true",
                    help="强制只读模式（只上传画面，不注入键鼠）")
    ap.add_argument("--allow-input", action="store_true",
                    help="⚠️ 允许注入键鼠（覆盖配置）。请先确认只读模式已跑通")
    ap.add_argument("--backend", choices=["auto", "dxcam", "mss"], default=None,
                    help="采集后端，覆盖配置")
    ap.add_argument("--check", action="store_true", help="只校验配置后退出")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：验证依赖、采集、注入、配置是否正常（不联网、不注入）")
    ap.add_argument("--no-clipboard", action="store_true",
                    help="禁用剪贴板同步（剪贴板可能含敏感内容）")
    ap.add_argument("--list-monitors", action="store_true",
                    help="只列出显示器及其编号然后退出（用于决定 config 里的 output）")
    args = ap.parse_args()

    if args.list_monitors:
        from agent import capture as capture_mod
        print()
        print("枚举到的显示器（编号即 config 里 [agent] output 的值）：")
        print(capture_mod.describe_monitors())
        print()
        print("提示：output 留空 = 采集主屏（推荐）。")
        return 0

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print("配置错误：%s" % e)
        return 2

    if args.readonly:
        cfg.agent.allow_input = False
    if args.allow_input:
        cfg.agent.allow_input = True
    if args.backend:
        cfg.agent.capture_backend = args.backend
    if args.no_clipboard:
        cfg.agent.clipboard_sync = False

    if args.selftest:
        return run_selftest(cfg)

    log = build_logger(cfg)

    print()
    print("=" * 72)
    print("  ALSPD-DESK  受控端 (Agent)  ——  运行在被控电脑上")
    print("=" * 72)
    print("  配置文件    : %s" % (cfg.path or "(未找到，使用默认值)"))
    print("  中继        : %s  端口 %s"
          % (cfg.common.relay_host or "(未配置)",
             ", ".join(str(p) for p in cfg.common.relay_ports)))
    print("  房间        : %s" % (cfg.common.room or "(未配置)"))
    print("  TLS         : %s" % ("启用" if cfg.tls.enabled else "禁用"))

    # 【保命措施】把只读/可注入状态做得非常显眼，避免误开
    if cfg.agent.allow_input:
        print("  键鼠注入    : ⚠️  已启用（Viewer 可以操作本机）")
        print("                急停热键 %s —— 按下即停止注入并断开" % cfg.agent.panic_hotkey)
    else:
        print("  键鼠注入    : ✅ 已禁用（只读模式：只上传画面，不注入键鼠）")

    problems = config_mod.validate_common(cfg)
    if problems:
        print("-" * 72)
        for p in problems:
            print("  ❌ %s" % p)
        print("=" * 72)
        return 2

    print("=" * 72)
    print()

    if args.check:
        print("  配置校验通过（--check 模式，未启动）")
        return 0

    session = AgentSession(cfg, log=lambda m: log.info(m))
    try:
        asyncio.run(session.run())
    except KeyboardInterrupt:
        print()
        log.info("收到 Ctrl-C，正在停止……")
        session.stop()
    except HandshakeError as e:
        log.error("连接失败：%s", e)
        return 1
    except Exception:
        log.exception("未预期的错误")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
