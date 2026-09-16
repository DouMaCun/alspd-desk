# -*- coding: utf-8 -*-
"""键鼠注入（Windows SendInput）与保命措施。

⚠️ 风险提示
-----------
这个模块会**真的操作本机键鼠**。而本项目的开发机就是被控机，
所以注入逻辑的 bug 有可能夺走开发者自己的键鼠。

因此本模块的设计原则是「**默认不伤人**」：

1. ``dry_run=True`` 时只做参数校验与记录，**完全不调用 SendInput**。
   所有单元测试都跑在 dry-run 下。
2. **急停热键**（``PanicHotkey``）用 ``RegisterHotKey`` 实现，
   不装键盘钩子、不拦输入，对系统影响最小，AV 也不敏感。
3. **本机活动检测**：注入后记录鼠标应有位置，下次注入前比对实际位置；
   若明显不符，说明本机有人在动鼠标 —— 立即暂停注入，把控制权让人。
   （不用钩子也能区分「注入的」和「本地手动」的移动。）

注入基础
--------
* 鼠标用 **绝对坐标 + 虚拟桌面**（``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``），
  这样多显示器也能正确定位；绝对移动会绕过「提高指针精确度」的加速度。
* 键盘发**扫描码**而非虚拟键码，尊重**目标机器**的键盘布局。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# ---------------------------------------------------------------- Win32 常量

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

BUTTON_FLAGS = {
    1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    3: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

IS_WIN = sys.platform == "win32"


# ---------------------------------------------------------------- ctypes 结构

if IS_WIN:
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                    ("wParamH", ctypes.c_ushort)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int
else:
    user32 = None


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """整个虚拟桌面的 ``(left, top, width, height)``。"""
    if not IS_WIN:
        return (0, 0, 1920, 1080)
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def primary_screen_size() -> Tuple[int, int]:
    if not IS_WIN:
        return (1920, 1080)
    return (user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN))


def get_cursor_pos() -> Optional[Tuple[int, int]]:
    if not IS_WIN:
        return None
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return (pt.x, pt.y)
    return None


# ---------------------------------------------------------------- 急停热键

_HOTKEY_NAMES = {
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20, "enter": 0x0D,
    "return": 0x0D, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "scrolllock": 0x91, "printscreen": 0x2C,
}
for _i in range(1, 25):
    _HOTKEY_NAMES["f%d" % _i] = 0x6F + _i          # F1..F24


def parse_hotkey(spec: str) -> Tuple[int, int]:
    """把 ``"ctrl+alt+shift+q"`` 解析成 ``(modifiers, vk)``。"""
    mods = 0
    vk = None
    for part in (spec or "").lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif part in _HOTKEY_NAMES:
            vk = _HOTKEY_NAMES[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        else:
            raise ValueError("无法识别的热键组成：%r" % part)
    if vk is None:
        raise ValueError("热键缺少主键：%r" % spec)
    return mods, vk


class PanicHotkey:
    """全局急停热键。

    用 ``RegisterHotKey`` 实现 —— **不安装键盘钩子**，只注册一个组合键，
    对系统的影响最小。只在按下时回调一次。

    如果注册失败（比如被别的程序占用），会明确报错，而不是静默失效。
    """

    def __init__(self, spec: str, callback: Callable[[], None], log=print):
        self.spec = spec
        self.callback = callback
        self.log = log
        self.modifiers, self.vk = parse_hotkey(spec)
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._running = False
        self.registered = False

    def start(self) -> bool:
        if not IS_WIN:
            self.log("[急停] 非 Windows 平台，热键不可用")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, name="panic-hotkey", daemon=True)
        self._thread.start()
        self._thread.join(timeout=3.0)
        return self.registered

    def _run(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()

        hotkey_id = 1
        ok = user32.RegisterHotKey(None, hotkey_id, self.modifiers | MOD_NOREPEAT, self.vk)
        if not ok:
            err = ctypes.get_last_error()
            self.log("[急停] ⚠️ 热键 %s 注册失败（错误码 %d）—— 可能已被其他程序占用。"
                     "请改用别的组合，或依赖「空闲自动断连」兜底。" % (self.spec, err))
            self.registered = False
            return

        self.registered = True
        self.log("[急停] 热键已注册：%s  —— 按下即停止注入并断开远程会话" % self.spec)

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                        ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                        ("time", ctypes.c_ulong), ("pt_x", ctypes.c_long),
                        ("pt_y", ctypes.c_long)]

        msg = MSG()
        try:
            while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.log("[急停] 🔴 热键被按下 —— 立即停止注入")
                    try:
                        self.callback()
                    except Exception as e:
                        self.log("[急停] 回调出错：%s: %s" % (type(e).__name__, e))
                    break
        finally:
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False
        if IS_WIN and self._thread_id:
            try:
                ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass


# ---------------------------------------------------------------- 注入器

class InputInjector:
    """键鼠注入器。

    ``dry_run=True``（默认在测试里使用）时只记录动作，不调用 SendInput —— 安全。
    """

    def __init__(self, monitor_rect: Tuple[int, int, int, int],
                 log: Callable[[str], None] = print,
                 dry_run: bool = False,
                 activity_guard: bool = True,
                 guard_threshold: int = 12,
                 guard_auto_resume: float = 3.0,
                 cursor_pos_fn: Optional[Callable[[], Optional[Tuple[int, int]]]] = None,
                 virtual_rect: Optional[Tuple[int, int, int, int]] = None):
        self.mon_left, self.mon_top, self.mon_w, self.mon_h = monitor_rect
        self.log = log
        self.dry_run = dry_run
        self.activity_guard = activity_guard
        self.guard_threshold = max(1, int(guard_threshold))
        self.guard_auto_resume = max(0.0, float(guard_auto_resume))
        # 允许注入「读光标位置」的实现，方便测试本机活动检测而不碰真实鼠标
        self._cursor_pos = cursor_pos_fn or get_cursor_pos

        # virtual_rect 可覆盖，让坐标换算在测试里可确定性验证
        self.virt_left, self.virt_top, self.virt_w, self.virt_h = \
            virtual_rect if virtual_rect else virtual_screen_rect()

        self.enabled = True
        self.paused_reason: Optional[str] = None
        self._paused_at = 0.0
        self._expected_cursor: Optional[Tuple[int, int]] = None

        self.calls: list = []            # dry_run 时记录
        self.stats: Dict[str, Any] = {
            "mouse_moves": 0, "buttons": 0, "wheels": 0, "keys": 0,
            "paused_events": 0, "send_failures": 0, "guard_trips": 0,
        }

    # ---------------------------------------------------------- 内部：发送

    def _send(self, *inputs) -> bool:
        """调用 SendInput。返回是否全部成功。"""
        if self.dry_run:
            self.calls.append(inputs)
            return True
        if not IS_WIN:
            return False
        arr = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            self.stats["send_failures"] += 1
            return False
        return True

    def _abs_coords(self, x_norm: float, y_norm: float) -> Tuple[int, int]:
        """归一化 0~1 -> 虚拟桌面绝对坐标（0~65535）。

        用虚拟桌面是为了支持多显示器。输出分辨率和缩放比例都无所谓，
        因为我们用的是**归一化坐标**。
        """
        px = self.mon_left + min(max(x_norm, 0.0), 1.0) * self.mon_w
        py = self.mon_top + min(max(y_norm, 0.0), 1.0) * self.mon_h
        # 绝对坐标映射到虚拟桌面：65535 对应最右边/最下边的像素
        ax = int(round((px - self.virt_left) * 65535.0 / max(1, self.virt_w - 1)))
        ay = int(round((py - self.virt_top) * 65535.0 / max(1, self.virt_h - 1)))
        return max(0, min(65535, ax)), max(0, min(65535, ay))

    def _mouse_input(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> "INPUT":
        return INPUT(type=INPUT_MOUSE,
                     union=_INPUTUNION(mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data,
                                                     dwFlags=flags, time=0, dwExtraInfo=0)))

    def _key_input(self, scancode: int, down: bool, extended: bool) -> "INPUT":
        flags = KEYEVENTF_SCANCODE
        if not down:
            flags |= KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(type=INPUT_KEYBOARD,
                     union=_INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=scancode & 0xFF,
                                                     dwFlags=flags, time=0, dwExtraInfo=0)))

    # ---------------------------------------------------------- 保命：暂停/恢复

    def pause(self, reason: str) -> None:
        if self.enabled:
            self.enabled = False
            self.paused_reason = reason
            self._paused_at = time.time()
            self.log("[保命] ⏸  已暂停键鼠注入：%s" % reason)

    def resume(self, reason: str = "手动恢复") -> None:
        if not self.enabled:
            self.enabled = True
            self.paused_reason = None
            # 恢复时重置基线，否则第一次比对必然误判
            self._expected_cursor = self._cursor_pos()
            self.log("[保命] ▶  已恢复键鼠注入：%s" % reason)

    def _check_activity_guard(self) -> bool:
        """本机活动检测：注入前比对鼠标实际位置与我们上次设置的位置。

        不需要键盘钩子就能区分「我们注入的移动」和「本机人手动的移动」。
        发现有人动鼠标就立刻让出控制权。
        """
        if not self.activity_guard or self._expected_cursor is None:
            return True
        actual = self._cursor_pos()
        if actual is None:
            return True
        dx = abs(actual[0] - self._expected_cursor[0])
        dy = abs(actual[1] - self._expected_cursor[1])
        if max(dx, dy) > self.guard_threshold:
            self.stats["guard_trips"] += 1
            self.pause("检测到本机有人在操作鼠标（实际 %s，预期 %s）—— 把控制权让给本机"
                       % (actual, self._expected_cursor))
            return False
        return True

    def _maybe_auto_resume(self) -> None:
        if self.enabled or not self.guard_auto_resume:
            return
        if self.paused_reason and "本机有人在操作鼠标" in self.paused_reason:
            if time.time() - self._paused_at >= self.guard_auto_resume:
                self.resume("本机已安静 %.0f 秒，自动恢复" % self.guard_auto_resume)

    # ---------------------------------------------------------- 对外注入接口

    def inject_mouse_move(self, x: float, y: float) -> bool:
        self._maybe_auto_resume()
        if not self.enabled or not self._check_activity_guard():
            self.stats["paused_events"] += 1
            return False
        ax, ay = self._abs_coords(x, y)
        ok = self._send(self._mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay))
        if ok:
            self.stats["mouse_moves"] += 1
            self._expected_cursor = self._cursor_pos() or self._expected_cursor
        return ok

    def inject_button(self, button: int, down: bool, x: float, y: float) -> bool:
        self._maybe_auto_resume()
        if not self.enabled or not self._check_activity_guard():
            self.stats["paused_events"] += 1
            return False
        flags = BUTTON_FLAGS.get(int(button))
        if flags is None:
            return False
        ax, ay = self._abs_coords(x, y)
        # 按下/抬起前先把光标移到目标位置，否则会在旧位置点击
        inputs = [self._mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)]
        inputs.append(self._mouse_input(flags[0] if down else flags[1]))
        ok = self._send(*inputs)
        if ok:
            self.stats["buttons"] += 1
            self._expected_cursor = self._cursor_pos() or self._expected_cursor
        return ok

    def inject_wheel(self, delta: int, x: float, y: float) -> bool:
        self._maybe_auto_resume()
        if not self.enabled or not self._check_activity_guard():
            self.stats["paused_events"] += 1
            return False
        if delta == 0:
            return False
        ax, ay = self._abs_coords(x, y)
        inputs = [self._mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)]
        # Windows 滚轮一格 = 120
        step = 120 if delta > 0 else -120
        count = min(10, max(1, int(abs(delta) / 120)))
        for _ in range(count):
            inputs.append(self._mouse_input(MOUSEEVENTF_WHEEL, data=step & 0xFFFFFFFF))
        ok = self._send(*inputs)
        if ok:
            self.stats["wheels"] += 1
            self._expected_cursor = self._cursor_pos() or self._expected_cursor
        return ok

    def inject_key(self, scancode: int, down: bool, extended: bool = False) -> bool:
        # 键盘不触发鼠标活动检测（本机用户可能在打字，但我们无法用位置判断）
        if not self.enabled:
            self.stats["paused_events"] += 1
            return False
        if not scancode:
            return False
        ok = self._send(self._key_input(scancode, down, extended))
        if ok:
            self.stats["keys"] += 1
        return ok

    # ---------------------------------------------------------- 统一入口

    def handle_event(self, ev: Dict[str, Any]) -> bool:
        """处理一条协议里的键鼠事件。返回是否真的注入了。"""
        kind = ev.get("t")
        try:
            if kind == "m":
                return self.inject_mouse_move(float(ev.get("x", 0.0)), float(ev.get("y", 0.0)))
            if kind == "b":
                return self.inject_button(int(ev.get("b", 1)), bool(ev.get("d")),
                                          float(ev.get("x", 0.0)), float(ev.get("y", 0.0)))
            if kind == "w":
                return self.inject_wheel(int(ev.get("w", 0)),
                                         float(ev.get("x", 0.0)), float(ev.get("y", 0.0)))
            if kind == "k":
                return self.inject_key(int(ev.get("sc", 0)), bool(ev.get("d")),
                                       bool(ev.get("ext", 0)))
        except (TypeError, ValueError) as e:
            self.log("[注入] 事件参数非法 %r：%s" % (ev, e))
        return False

    def release_all(self) -> None:
        """松开所有可能被按住的键与鼠标键。断线时必须调用，否则会留下「卡住的按键」。"""
        if self.dry_run:
            self.calls.append(("release_all",))
            return
        inputs = []
        for _down, up in BUTTON_FLAGS.values():
            inputs.append(self._mouse_input(up))
        # 常见修饰键 + 主要按键，统统补一个抬起，避免远端卡键
        for sc in (0x1D, 0x2A, 0x36, 0x38, 0x1D | 0x100, 0x38 | 0x100):
            ext = bool(sc & 0x100)
            inputs.append(self._key_input(sc & 0xFF, False, ext))
        if inputs:
            try:
                self._send(*inputs)
                self.log("[注入] 已松开所有按键（防止远端卡键）")
            except Exception as e:
                self.log("[注入] 释放按键失败：%s" % e)
