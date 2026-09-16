#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_input_safety.py —— 键鼠注入与保命措施的测试（**全程 dry-run，不动真实键鼠**）

为什么必须 dry-run
------------------
本项目的开发机就是被控机。如果测试真的调用 ``SendInput``，
一旦逻辑有 bug 就可能夺走开发者自己的键鼠。所以：

* 所有测试都构造 ``InputInjector(dry_run=True)``，
  ``_send`` 只把要发送的 ``INPUT`` 结构记录下来，**绝不调用 ``SendInput``**。
* 「本机活动检测」通过注入一个假的光标位置函数来测，不碰真实鼠标。
* 「急停热键」只测参数解析（真的 ``RegisterHotKey`` 需要按键盘才能验证，
  这一条留给你手动验证）。

覆盖范围
--------
1. 热键字符串解析
2. 归一化坐标 -> 虚拟桌面绝对坐标（含多显示器偏移、边界钳制）
3. dry-run 安全性：绝不调用 SendInput
4. 鼠标按键映射、滚轮换算
5. 键盘扫描码 / 扩展键 / 抬起标志
6. 本机活动检测：本机有人动鼠标 -> 立即暂停注入
7. 暂停自动恢复
8. 断线时松开所有按键（防远端卡键）

用法
----
    python tools/test_input_safety.py
"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common.console import setup_console         # noqa: E402

setup_console()

from agent import input as inp                   # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


def mi(inp_obj):
    return inp_obj.union.mi


def ki(inp_obj):
    return inp_obj.union.ki


def make(monitor=(0, 0, 1920, 1080), virtual=(0, 0, 1920, 1080), cursor=None, **kw):
    """构造 dry-run 注入器。默认光标位置固定返回 (0,0) 以便活动检测不误触发。"""
    cur = cursor if cursor is not None else (lambda: (0, 0))
    return inp.InputInjector(monitor, log=lambda m: None, dry_run=True,
                             cursor_pos_fn=cur, virtual_rect=virtual, **kw)


class FakeDesktop:
    """模拟真实桌面光标，让活动检测的测试**符合真实语义**。

    真实环境下：``SendInput`` 移完光标后，``GetCursorPos`` 会返回新位置。
    用一个返回常量的假函数是模拟不出来的 —— 会导致「自己注入的移动」被误判成
    「本机有人在动鼠标」。所以这里拦截 ``_send``，解析出绝对坐标来真正移动假光标，
    这样测试才测得到真实行为。

    调 ``divert()`` 用来模拟「本机用户把鼠标挪走了」。
    """

    def __init__(self, injector: "inp.InputInjector",
                 virtual=(0, 0, 1920, 1080), start=(0, 0)):
        self.j = injector
        self.virtual = virtual
        self.pos = start
        self.sent = []
        # 用假光标 + 拦截发送，绝不碰真实键鼠
        injector._cursor_pos = lambda: self.pos
        injector._send = self._send

    def _to_pixels(self, ax, ay):
        vx, vy, vw, vh = self.virtual
        px = vx + ax * (vw - 1) / 65535.0
        py = vy + ay * (vh - 1) / 65535.0
        return (int(round(px)), int(round(py)))

    def _send(self, *inputs):
        self.sent.append(inputs)
        self.j.calls.append(inputs)
        for i in inputs:
            if i.type == inp.INPUT_MOUSE:
                f = i.union.mi.dwFlags
                if (f & inp.MOUSEEVENTF_MOVE) and (f & inp.MOUSEEVENTF_ABSOLUTE):
                    self.pos = self._to_pixels(i.union.mi.dx, i.union.mi.dy)
        return True

    def divert(self, x, y):
        """模拟本机用户把鼠标挪到别处。"""
        self.pos = (x, y)


# ============================================================ 1. 热键解析

def test_hotkey_parse():
    print("[1] 急停热键解析")
    m, vk = inp.parse_hotkey("ctrl+alt+shift+q")
    check("ctrl+alt+shift+q 解析正确",
          m == (inp.MOD_CONTROL | inp.MOD_ALT | inp.MOD_SHIFT) and vk == ord("Q"),
          "mods=0x%X vk=0x%X" % (m, vk))

    m, vk = inp.parse_hotkey("Ctrl+Alt+Q")
    check("大小写不敏感", m == (inp.MOD_CONTROL | inp.MOD_ALT) and vk == ord("Q"))

    m, vk = inp.parse_hotkey("ctrl+shift+F12")
    check("F12 解析正确", vk == 0x7B, "vk=0x%X" % vk)

    m, vk = inp.parse_hotkey("win+alt+esc")
    check("win 修饰键 + esc", m & inp.MOD_WIN and vk == 0x1B)

    for bad in ("", "ctrl", "ctrl+unknownkey", "ctrl+alt"):
        try:
            inp.parse_hotkey(bad)
            check("非法热键 %r 应报错" % bad, False, "居然解析成功了")
        except ValueError:
            check("非法热键 %r 正确报错" % bad, True)
    print()


# ============================================================ 2. 坐标换算

def test_coords():
    print("[2] 归一化坐标 -> 绝对坐标")
    j = make()

    ax, ay = j._abs_coords(0.0, 0.0)
    check("左上角 (0,0) -> 绝对 (0,0)", (ax, ay) == (0, 0), "得到 %s" % ((ax, ay),))

    ax, ay = j._abs_coords(1.0, 1.0)
    check("右下角 (1,1) -> 绝对 (65535,65535)", (ax, ay) == (65535, 65535),
          "得到 %s" % ((ax, ay),))

    ax, ay = j._abs_coords(0.5, 0.5)
    # Windows 的映射是 v*65535/(尺寸-1)。注意 x 和 y 的分母不同（1919 vs 1079），
    # 所以两轴的期望值并不相等 —— 960 不是 [0,1919] 的正中，540 也不是 [0,1079] 的正中。
    exp_x = int(round(960 * 65535.0 / (1920 - 1)))
    exp_y = int(round(540 * 65535.0 / (1080 - 1)))
    check("中心 (0.5,0.5) 映射正确", ax == exp_x and ay == exp_y,
          "得到 (%d,%d)，期望 (%d,%d)" % (ax, ay, exp_x, exp_y))

    # 越界必须钳制，否则会点到屏幕外
    ax, ay = j._abs_coords(-0.5, 1.8)
    check("越界坐标被钳制到 [0,1]", (ax, ay) == (0, 65535), "得到 %s" % ((ax, ay),))

    # 多显示器：副屏在 (1920,0)，虚拟桌面 3840x1080
    j2 = make(monitor=(1920, 0, 1920, 1080), virtual=(0, 0, 3840, 1080))
    ax, ay = j2._abs_coords(0.0, 0.0)
    expect = int(round(1920 * 65535.0 / 3839))
    check("副屏 (1920,0) 的左上角映射到虚拟桌面中段",
          abs(ax - expect) <= 1 and ay == 0,
          "得到 (%d,%d)，期望 (%d,0)" % (ax, ay, expect))
    ax2, ay2 = j2._abs_coords(1.0, 1.0)
    check("副屏右下角 -> 绝对右下角", (ax2, ay2) == (65535, 65535),
          "得到 %s" % ((ax2, ay2),))
    print()


# ============================================================ 3. dry-run 安全性

def test_dry_run_is_safe():
    print("[3] dry-run 安全性（最关键的一条）")
    j = make(activity_guard=False)
    n_before = len(j.calls)
    j.inject_mouse_move(0.3, 0.4)
    j.inject_button(1, True, 0.3, 0.4)
    j.inject_button(1, False, 0.3, 0.4)
    j.inject_wheel(120, 0.5, 0.5)
    j.inject_key(0x1E, True)
    j.inject_key(0x1E, False)
    check("dry-run 下所有调用都被记录下来", len(j.calls) - n_before >= 6,
          "记录 %d 条" % (len(j.calls) - n_before))
    check("dry-run 下没有真的发送（stats 仍然计数）",
          j.stats["mouse_moves"] == 1 and j.stats["buttons"] == 2
          and j.stats["wheels"] == 1 and j.stats["keys"] == 2,
          "stats=%s" % {k: j.stats[k] for k in ("mouse_moves", "buttons", "wheels", "keys")})
    print()


# ============================================================ 4/5. 鼠标与键盘

def test_mouse_and_keys():
    print("[4] 鼠标按键与滚轮")

    j = make(activity_guard=False)
    j.inject_button(1, True, 0.1, 0.1)
    flags = [mi(x).dwFlags for x in j.calls[-1]]
    check("左键按下：先移动再按下",
          flags[0] & inp.MOUSEEVENTF_MOVE and flags[1] == inp.MOUSEEVENTF_LEFTDOWN,
          "flags=%s" % [hex(f) for f in flags])

    j = make(activity_guard=False)
    j.inject_button(2, False, 0.1, 0.1)
    check("右键抬起", mi(j.calls[-1][1]).dwFlags == inp.MOUSEEVENTF_RIGHTUP)

    j = make(activity_guard=False)
    j.inject_button(3, True, 0.1, 0.1)
    check("中键按下", mi(j.calls[-1][1]).dwFlags == inp.MOUSEEVENTF_MIDDLEDOWN)

    j = make(activity_guard=False)
    ok = j.inject_button(9, True, 0.1, 0.1)
    check("未知按键被拒绝", ok is False)

    j = make(activity_guard=False)
    j.inject_wheel(120, 0.5, 0.5)
    wheels = [x for x in j.calls[-1] if mi(x).dwFlags == inp.MOUSEEVENTF_WHEEL]
    check("滚轮一格 (+120) -> 1 次滚轮事件", len(wheels) == 1,
          "得到 %d 次" % len(wheels))

    j = make(activity_guard=False)
    j.inject_wheel(-360, 0.5, 0.5)
    wheels = [x for x in j.calls[-1] if mi(x).dwFlags == inp.MOUSEEVENTF_WHEEL]
    check("滚轮 -360 -> 3 次向下滚动", len(wheels) == 3, "得到 %d 次" % len(wheels))
    check("向下滚动的 data 为负",
          all(mi(x).mouseData >= 0xFFFFFF00 for x in wheels),
          "data=%s" % [hex(mi(x).mouseData) for x in wheels])

    j = make(activity_guard=False)
    check("滚轮 delta=0 不发事件", j.inject_wheel(0, 0.5, 0.5) is False)

    print()
    print("[5] 键盘（扫描码 / 扩展键 / 抬起）")
    j = make(activity_guard=False)
    j.inject_key(0x1E, True)          # 'A' 的扫描码
    k = ki(j.calls[-1][0])
    check("按下：带 SCANCODE 标志、无 KEYUP",
          (k.dwFlags & inp.KEYEVENTF_SCANCODE) and not (k.dwFlags & inp.KEYEVENTF_KEYUP)
          and k.wScan == 0x1E,
          "scan=0x%X flags=0x%X" % (k.wScan, k.dwFlags))

    j = make(activity_guard=False)
    j.inject_key(0x1E, False)
    k = ki(j.calls[-1][0])
    check("抬起：带 KEYUP 标志", k.dwFlags & inp.KEYEVENTF_KEYUP, "flags=0x%X" % k.dwFlags)

    j = make(activity_guard=False)
    j.inject_key(0x4B, True, extended=True)     # 左方向键
    k = ki(j.calls[-1][0])
    check("扩展键：带 EXTENDEDKEY 标志", k.dwFlags & inp.KEYEVENTF_EXTENDEDKEY,
          "flags=0x%X" % k.dwFlags)

    j = make(activity_guard=False)
    check("扫描码为 0 时拒绝", j.inject_key(0, True) is False)

    check("用扫描码而非虚拟键码（wVk 必须为 0）", ki(_first_key()).wVk == 0,
          "wVk=%d wScan=0x%X" % (ki(_first_key()).wVk, ki(_first_key()).wScan))


def _first_key():
    j = make(activity_guard=False)
    j.inject_key(0x1E, True)
    return j.calls[-1][0]


# ============================================================ 6/7. 活动检测

def test_activity_guard():
    print("[6] 本机活动检测：本机有人动鼠标 -> 立即让出控制权")

    # ---- 本机没人动：光标始终跟着我们的注入走，不该触发 ----
    j = inp.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True,
                          guard_auto_resume=0.0, virtual_rect=(0, 0, 1920, 1080))
    desk = FakeDesktop(j)
    ok1 = j.inject_mouse_move(0.5, 0.5)
    ok2 = j.inject_mouse_move(0.6, 0.5)
    ok3 = j.inject_mouse_move(0.7, 0.6)
    check("光标与预期一致时连续注入都成功", ok1 and ok2 and ok3 and j.enabled,
          "enabled=%s guard_trips=%d cursor=%s" % (j.enabled, j.stats["guard_trips"], desk.pos))
    check("未误触发活动检测", j.stats["guard_trips"] == 0,
          "trips=%d" % j.stats["guard_trips"])

    # ---- 本机用户把鼠标挪走 ----
    desk.divert(200, 200)
    blocked = j.inject_mouse_move(0.8, 0.5)
    check("光标被本机挪走 -> 该次注入被拦下", blocked is False)
    check("检测触发后进入暂停状态", j.enabled is False and j.stats["guard_trips"] >= 1,
          "enabled=%s trips=%d" % (j.enabled, j.stats["guard_trips"]))
    check("暂停原因说明是「本机有人在操作鼠标」",
          j.paused_reason is not None and "本机有人在操作鼠标" in j.paused_reason,
          "%s" % j.paused_reason)
    check("暂停期间键盘也被拦下", j.inject_key(0x1E, True) is False)

    # ---- 按键也该被拦下（不依赖位置判断，但暂停必须对所有输入生效）----
    j2 = inp.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True,
                           virtual_rect=(0, 0, 1920, 1080))
    FakeDesktop(j2)
    j2.pause("测试")
    check("暂停后鼠标事件也被拦下", j2.inject_mouse_move(0.5, 0.5) is False)
    check("暂停事件计入 paused_events", j2.stats["paused_events"] >= 1,
          "paused_events=%d" % j2.stats["paused_events"])

    print()
    print("[7] 暂停后的自动恢复")
    import time as _t
    j3 = inp.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True,
                           guard_auto_resume=0.2, virtual_rect=(0, 0, 1920, 1080))
    d3 = FakeDesktop(j3)
    j3.inject_mouse_move(0.3, 0.3)
    d3.divert(50, 50)
    j3.inject_mouse_move(0.3, 0.3)
    check("先进入暂停", j3.enabled is False, "enabled=%s" % j3.enabled)
    _t.sleep(0.3)
    d3.pos = (0, 0)                      # 本机安静了
    ok = j3.inject_mouse_move(0.3, 0.3)
    check("安静超过阈值后自动恢复", ok is True and j3.enabled is True,
          "enabled=%s" % j3.enabled)

    j4 = inp.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True,
                           guard_auto_resume=0.0, virtual_rect=(0, 0, 1920, 1080))
    d4 = FakeDesktop(j4)
    j4.inject_mouse_move(0.3, 0.3)
    d4.divert(900, 900)
    j4.inject_mouse_move(0.3, 0.3)
    _t.sleep(0.35)
    j4.inject_mouse_move(0.3, 0.3)
    check("auto_resume=0 时保持暂停（需手动恢复）", j4.enabled is False,
          "enabled=%s" % j4.enabled)
    j4.resume()
    check("手动恢复后可以继续注入", j4.enabled is True and j4.inject_mouse_move(0.3, 0.3))

    j5 = inp.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True,
                           activity_guard=False, virtual_rect=(0, 0, 1920, 1080))
    FakeDesktop(j5)
    j5.inject_mouse_move(0.3, 0.3)
    j5._cursor_pos = lambda: (1700, 900)     # 大幅偏离
    ok = j5.inject_mouse_move(0.4, 0.4)
    check("关闭活动检测后不受光标偏移影响", ok is True and j5.enabled is True)
    print()


# ============================================================ 8. 断开释放

def test_release_all():
    print("[8] 断开时松开所有按键（防远端卡键）")
    j = make(activity_guard=False)
    j.release_all()
    check("release_all 在 dry-run 下被记录", j.calls and j.calls[-1] == ("release_all",),
          "%r" % (j.calls[-1],))

    # 非 dry-run 时检查生成的 INPUT：不能真的发送，所以直接看 _send 的记录
    j2 = inp.InputInjector((0, 0, 1920, 1080), log=lambda m: None, dry_run=True,
                           virtual_rect=(0, 0, 1920, 1080))
    j2.dry_run = False
    recorded = []
    j2._send = lambda *ins: (recorded.extend(ins), True)[1]     # 拦截，避免真实注入
    j2.release_all()
    ups = [x for x in recorded if x.type == inp.INPUT_KEYBOARD]
    mouse_ups = [x for x in recorded if x.type == inp.INPUT_MOUSE]
    check("包含键盘抬起事件", len(ups) > 0, "%d 个" % len(ups))
    check("所有键盘事件都带 KEYUP", all(ki(x).dwFlags & inp.KEYEVENTF_KEYUP for x in ups))
    check("包含鼠标按键抬起事件", len(mouse_ups) >= 3, "%d 个" % len(mouse_ups))
    print()


# ============================================================ main

def main() -> int:
    print()
    print("=" * 74)
    print("  ALSPD-DESK  Phase 2  键鼠注入与保命措施测试")
    print("=" * 74)
    print("  ⚠️  全程 dry-run：只校验参数、记录调用，**不调用 SendInput**，")
    print("      不会操作本机键鼠，可以放心运行。")
    print("-" * 74)

    test_hotkey_parse()
    test_coords()
    test_dry_run_is_safe()
    test_mouse_and_keys()
    test_activity_guard()
    test_release_all()

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("=" * 74)
    if passed == total:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  注入参数换算、按键映射、活动检测、断开释放都正确。")
        print("  注：急停热键的真实按键效果需要手动验证（运行 Agent 后按 Ctrl+Alt+Shift+Q）。")
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
