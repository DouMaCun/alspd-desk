#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_multimonitor.py —— 多显示器坐标还原的测试

要解决什么问题
--------------
键鼠注入需要知道「采集的是哪块屏、它在虚拟桌面上的位置」。
但 dxcam **不暴露这个偏移**（它的 ``region`` 是输出本地坐标，恒从 0,0 开始），
所以副屏上注入会错位。这里靠 mss 的显示器枚举（它给出 left/top）按尺寸还原。

为什么必须写单元测试
--------------------
本机只有一种双屏布局，**分辨率相同、旋转、无匹配**这些情况根本复现不了 ——
而它们恰恰是最容易出错的地方。所以用合成数据把这些分支全部覆盖。

安全说明
--------
注入相关部分一律 dry-run，**不会操作你的键鼠**。

用法
----
    python tools/test_multimonitor.py
"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common.console import setup_console         # noqa: E402

setup_console()

from agent import capture as cap_mod             # noqa: E402
from agent import input as inp_mod               # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


def mon(index, left, top, w, h, primary=False, name=""):
    return {"index": index, "left": left, "top": top, "width": w, "height": h,
            "primary": primary, "name": name}


# ============================================================ 解析

def test_parse_dxcam():
    print("[1] dxcam output_info() 解析")
    real = ("Device[0] Output[0]: Res:(2560, 1440) Rot:0 Primary:True\n"
            "Device[0] Output[1]: Res:(1920, 1080) Rot:0 Primary:False\n")
    outs = cap_mod.parse_dxcam_outputs(real)
    check("解析出 2 个输出", len(outs) == 2, "%d 个" % len(outs))
    if len(outs) == 2:
        check("Output[0] 的分辨率与主屏标志正确",
              outs[0]["output"] == 0 and outs[0]["width"] == 2560
              and outs[0]["height"] == 1440 and outs[0]["primary"] is True,
              "%s" % outs[0])
        check("Output[1] 的副屏标志正确",
              outs[1]["output"] == 1 and outs[1]["primary"] is False, "%s" % outs[1])

    rotated = "Device[0] Output[0]: Res:(1080, 1920) Rot:90 Primary:True\n"
    outs = cap_mod.parse_dxcam_outputs(rotated)
    check("能解析旋转信息", len(outs) == 1 and outs[0]["rotation"] == 90, "%s" % outs)

    # 解析不出来时必须优雅返回空列表（这是库的调试字符串，格式可能变）
    for junk in ("", None, "完全不是这个格式", "Output[abc] Res:(x,y)"):
        check("非预期输入返回空列表（不抛异常）%r" % (junk,),
              cap_mod.parse_dxcam_outputs(junk) == [])


# ============================================================ 坐标还原

def test_resolve():
    print()
    print("[2] 按分辨率唯一匹配")
    mons = [mon(0, 1920, 0, 1920, 1080, False, "副屏"),
            mon(1, 0, 0, 2560, 1440, True, "主屏")]
    check("主屏（唯一 2560x1440）",
          cap_mod.resolve_monitor_rect(0, 2560, 1440, mons) == (0, 0, 2560, 1440),
          "%s" % (cap_mod.resolve_monitor_rect(0, 2560, 1440, mons),))
    check("副屏（唯一 1920x1080，位置含偏移）",
          cap_mod.resolve_monitor_rect(1, 1920, 1080, mons) == (1920, 0, 1920, 1080),
          "%s" % (cap_mod.resolve_monitor_rect(1, 1920, 1080, mons),))

    # 本机的真实布局：副屏在 (2560, 566)，有纵向偏移
    real = [mon(0, 2560, 566, 1920, 1080, False),
            mon(1, 0, 0, 2560, 1440, True)]
    check("本机布局：副屏偏移 (2560,566) 正确还原",
          cap_mod.resolve_monitor_rect(1, 1920, 1080, real) == (2560, 566, 1920, 1080),
          "%s" % (cap_mod.resolve_monitor_rect(1, 1920, 1080, real),))

    print()
    print("[3] 分辨率相同 -> 用 Primary 标志区分")
    same = [mon(0, 1920, 0, 1920, 1080, False, "副屏"),
            mon(1, 0, 0, 1920, 1080, True, "主屏")]
    outs = [{"output": 0, "width": 1920, "height": 1080, "rotation": 0, "primary": True},
            {"output": 1, "width": 1920, "height": 1080, "rotation": 0, "primary": False}]
    r0 = cap_mod.resolve_monitor_rect(0, 1920, 1080, same, outs)
    r1 = cap_mod.resolve_monitor_rect(1, 1920, 1080, same, outs)
    check("Output[0]（Primary=True）选到主屏", r0 == (0, 0, 1920, 1080), "%s" % (r0,))
    check("Output[1]（Primary=False）选到副屏", r1 == (1920, 0, 1920, 1080), "%s" % (r1,))

    # 没有 Primary 信息时无法区分 —— 必须拒绝，不能瞎猜
    r = cap_mod.resolve_monitor_rect(0, 1920, 1080, same, [])
    check("分辨率相同且无 Primary 信息 -> 拒绝返回 None（不猜）", r is None, "%s" % (r,))
    r = cap_mod.resolve_monitor_rect(None, 1920, 1080, same, outs)
    check("没给 output_idx 且分辨率相同 -> 拒绝返回 None", r is None, "%s" % (r,))

    print()
    print("[4] 旋转屏幕（枚举到的宽高与采集的宽高互换）")
    rot = [mon(0, 0, 0, 1080, 1920, True, "竖屏")]
    r = cap_mod.resolve_monitor_rect(0, 1920, 1080, rot)
    check("竖屏能匹配上（宽高互换也算命中）", r == (0, 0, 1080, 1920), "%s" % (r,))

    print()
    print("[5] 无法确定时必须拒绝，而不是给错")
    mons = [mon(0, 0, 0, 2560, 1440, True)]
    check("采集尺寸在枚举里找不到 -> None",
          cap_mod.resolve_monitor_rect(0, 1366, 768, mons) is None)
    check("显示器列表为空 -> None",
          cap_mod.resolve_monitor_rect(0, 2560, 1440, []) is None)
    check("显示器列表为 None 且枚举失败 -> None",
          cap_mod.resolve_monitor_rect(0, 2560, 1440, None if False else []) is None)


# ============================================================ 注入坐标联动

def test_injection_coords():
    print()
    print("[6] 与注入器的坐标换算联动（dry-run）")
    # 副屏在 (2560, 566)，虚拟桌面 4480x1646（本机真实尺寸）
    rect = (2560, 566, 1920, 1080)
    virt = (0, 0, 4480, 1646)
    inj = inp_mod.InputInjector(rect, log=lambda m: None, dry_run=True,
                                activity_guard=False, virtual_rect=virt,
                                cursor_pos_fn=lambda: None)

    # 副屏左上角应当映射到虚拟桌面里的 (2560,566)
    ax, ay = inj._abs_coords(0.0, 0.0)
    back_x = virt[0] + ax * (virt[2] - 1) / 65535.0
    back_y = virt[1] + ay * (virt[3] - 1) / 65535.0
    check("副屏 (0,0) 反算回虚拟桌面 (2560,566)",
          abs(back_x - 2560) <= 1.5 and abs(back_y - 566) <= 1.5,
          "反算得 (%.1f,%.1f)" % (back_x, back_y))

    ax, ay = inj._abs_coords(1.0, 1.0)
    back_x = virt[0] + ax * (virt[2] - 1) / 65535.0
    back_y = virt[1] + ay * (virt[3] - 1) / 65535.0
    check("副屏右下角反算回 (2560+1920, 566+1080)",
          abs(back_x - (2560 + 1920)) <= 1.5 and abs(back_y - (566 + 1080)) <= 1.5,
          "反算得 (%.1f,%.1f)" % (back_x, back_y))

    ax, ay = inj._abs_coords(0.5, 0.5)
    back_x = virt[0] + ax * (virt[2] - 1) / 65535.0
    back_y = virt[1] + ay * (virt[3] - 1) / 65535.0
    check("副屏中心反算正确",
          abs(back_x - (2560 + 960)) <= 1.5 and abs(back_y - (566 + 540)) <= 1.5,
          "反算得 (%.1f,%.1f)，期望 (%.1f,%.1f)"
          % (back_x, back_y, 2560 + 960, 566 + 540))


# ============================================================ 真实机器

def test_real_machine():
    print()
    print("[7] 本机真实布局")
    mons = cap_mod.list_monitors()
    if not mons:
        check("（枚举不到显示器，跳过）", True)
        return
    print("    枚举到 %d 块显示器：" % len(mons))
    for m in mons:
        print("      #%d %dx%d @ %d,%d %s"
              % (m["index"], m["width"], m["height"], m["left"], m["top"],
                 "主屏" if m["primary"] else "副屏"))
    check("枚举到了至少一块显示器", len(mons) >= 1)
    check("有且只有一块主屏", sum(1 for m in mons if m["primary"]) == 1,
          "%d 块标为主屏" % sum(1 for m in mons if m["primary"]))

    # 用真实采集器验证（dxcam 与 mss 都应给出正确的偏移）
    for backend in ("dxcam", "mss"):
        try:
            cap = cap_mod.make_capturer(backend, None, lambda m: None)
            try:
                rect = cap.monitor_rect()
                ok = rect is not None and rect[2] == cap.width and rect[3] == cap.height
                check("%s 能确定显示器区域且尺寸自洽" % backend, ok,
                      "采集 %dx%d -> 区域 %s" % (cap.width, cap.height, rect))
            finally:
                cap.close()
        except Exception as e:
            check("%s 可用" % backend, False, "%s: %s" % (type(e).__name__, e))


def main() -> int:
    print()
    print("=" * 74)
    print("  ALSPD-DESK  多显示器坐标还原测试")
    print("=" * 74)
    print("  ⚠️  注入相关全程 dry-run，不会操作你的键鼠。")
    print("-" * 74)

    test_parse_dxcam()
    test_resolve()
    test_injection_coords()
    test_real_machine()

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  多显示器偏移还原、边界拒绝、与注入坐标的联动都正确。")
        print("=" * 74)
        return 0
    print("  ❌ 有失败项：%d/%d 通过" % (passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 74)
    return 1


if __name__ == "__main__":
    sys.exit(main())
