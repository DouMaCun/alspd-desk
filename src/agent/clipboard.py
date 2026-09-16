# -*- coding: utf-8 -*-
"""剪贴板同步（当前只做**纯文本**）。

为什么需要它
------------
远程办公里复制粘贴是刚需 —— 在公司电脑上复制一段代码/链接，回家想粘到自己机器上，
反之亦然。没有它，远程操作会变得很别扭。

回环（echo）问题
----------------
剪贴板是双向同步的，最容易出的 bug 是**回环**：
A 端设置剪贴板 → 监听到变化 → 同步给 B → B 设置剪贴板 → B 监听到变化 → 又同步回 A → …
无限互相覆盖。

处理办法：**写入剪贴板的同时，把写入内容记为「已见」**。
这样监听线程看到的内容就等于「已见」，不会再次上报。
关键是这个「写入 + 记录」必须和「读取 + 比对」互斥（用同一把锁），
否则会出现「监听到旧内容又发回去」的竞态。

安全提示
--------
剪贴板可能含有敏感内容（从密码管理器复制的口令等）。
两端**都是你自己**，所以默认开启是合理的；如果不想同步，
把 Agent 的 ``clipboard_sync`` 设为 false（或用 Viewer 的 ``--no-clipboard``）。
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

IS_WIN = sys.platform == "win32"

# 单条剪贴板内容上限。过大的内容（比如整篇长文）会拖慢传输，
# 而且远端解密后也要占内存。
MAX_CLIPBOARD_LEN = 1_000_000


def read_clipboard_text() -> Optional[str]:
    """读 Windows 剪贴板的纯文本。拿不到返回 None（例如被别的程序占用）。"""
    if not IS_WIN:
        return None
    try:
        import win32clipboard
        import win32con
    except ImportError:
        return None
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        # 剪贴板被别的程序独占，稍后再试
        return None
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return None
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return data if isinstance(data, str) else None
    except Exception:
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def write_clipboard_text(text: str) -> bool:
    """写 Windows 剪贴板的纯文本。成功返回 True。"""
    if not IS_WIN:
        return False
    try:
        import win32clipboard
        import win32con
    except ImportError:
        return False
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True
    except Exception:
        return False
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


class ClipboardWatcher:
    """轮询本机剪贴板，发现**外部**变化时回调。

    ``set_text()`` 用于「因为远方同步而写入」的场景 —— 它会把内容记为已见，
    避免又把刚写进去的东西同步回去。
    """

    def __init__(self, on_change: Callable[[str], None],
                 log: Callable[[str], None] = print,
                 interval: float = 0.6,
                 enabled: bool = True,
                 read_fn: Optional[Callable[[], Optional[str]]] = None,
                 write_fn: Optional[Callable[[str], bool]] = None):
        self.on_change = on_change
        self.log = log
        self.interval = max(0.1, float(interval))
        self.enabled = enabled and IS_WIN
        # 允许注入读写实现：这样「回环防护」能在假后端上做确定性测试，
        # 而不会去动你真实的剪贴板内容
        self._read = read_fn or read_clipboard_text
        self._write = write_fn or write_clipboard_text

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last: Optional[str] = None
        self.stats = {"sent": 0, "applied": 0, "skipped_too_long": 0,
                      "write_failed": 0, "read_failed": 0}

    # ---------------------------------------------------------- 生命周期

    def _safe_read(self) -> Optional[str]:
        """读剪贴板，任何异常都吃掉并返回 None。

        Windows 上 ``OpenClipboard`` 在别的程序占用剪贴板时会失败。
        如果让异常冒出去，**监听线程会静默死掉** —— 同步从此失效而且毫无提示，
        这种问题极难排查。所以这里必须兜住。
        """
        try:
            value = self._read()
        except Exception as e:
            self.stats["read_failed"] += 1
            n = self.stats["read_failed"]
            # 只在前几次和之后每 100 次打日志，避免刷屏
            if n <= 3 or n % 100 == 0:
                self.log("[剪贴板] 读取失败（第 %d 次，通常是别的程序正占用剪贴板）：%s"
                         % (n, type(e).__name__))
            return None
        return value if isinstance(value, str) else None

    def start(self) -> None:
        if not self.enabled:
            self.log("[剪贴板] 已禁用（或非 Windows 平台）")
            return
        # 以当前剪贴板内容为基线：启动时不要把「本来就有的内容」当成新变化发出去
        with self._lock:
            self._last = self._safe_read()
        self._thread = threading.Thread(target=self._run, name="clipboard", daemon=True)
        self._thread.start()
        self.log("[剪贴板] 已启用（纯文本，双向同步）")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    # ---------------------------------------------------------- 内部

    def _run(self) -> None:
        while not self._stop.is_set():
            text = None
            changed = False
            # 读取与比对必须和 set_text 的「写入 + 记录」互斥，
            # 否则会出现「读到旧值 → 认为变了 → 把旧内容发回去」的竞态
            try:
                with self._lock:
                    text = self._safe_read()
                    if text is not None and text != self._last:
                        self._last = text
                        changed = True
            except Exception as e:
                # 最后一道兜底：无论如何都不能让监听线程死掉
                self.log("[剪贴板] 监听循环异常（已忽略并继续）：%s: %s"
                         % (type(e).__name__, e))

            if changed and text:
                if len(text) > MAX_CLIPBOARD_LEN:
                    self.stats["skipped_too_long"] += 1
                    self.log("[剪贴板] 内容过大（%d 字符），跳过同步" % len(text))
                else:
                    self.stats["sent"] += 1
                    try:
                        self.on_change(text)
                    except Exception as e:
                        self.log("[剪贴板] 上报失败：%s: %s" % (type(e).__name__, e))

            self._stop.wait(self.interval)

    # ---------------------------------------------------------- 对外

    def set_text(self, text: str) -> bool:
        """把远方的剪贴板内容写到本机（并防止回环）。"""
        if not self.enabled:
            return False
        if len(text) > MAX_CLIPBOARD_LEN:
            self.stats["skipped_too_long"] += 1
            return False
        with self._lock:
            ok = self._write(text)
            if ok:
                # 关键：写入的内容同时记为「已见」，监听线程就不会再把它发回去
                self._last = text
                self.stats["applied"] += 1
            else:
                self.stats["write_failed"] += 1
        if not ok:
            self.log("[剪贴板] 写入本机剪贴板失败（可能被其他程序占用）")
        return ok
