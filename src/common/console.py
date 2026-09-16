# -*- coding: utf-8 -*-
"""控制台编码处理。

Windows 控制台默认是 GBK，输出中文/符号会直接抛 UnicodeEncodeError。
这里统一切到 UTF-8，同时兼顾两种情况：

* 直接开控制台 —— 改 Windows 控制台代码页
* 输出被重定向到管道/文件 —— 改 Python 流的编码

Linux（VPS）本来就是 UTF-8，调用本函数是无害的兜底。
"""

from __future__ import annotations

import sys


def setup_console() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
