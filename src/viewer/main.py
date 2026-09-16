#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""viewer/main.py —— 控制端入口（运行在家里那台电脑上）

结构
----
::

    Qt 主线程                              后台线程
    ────────────────                      ────────────────
    ViewerWindow（绘制 / 捕获键鼠）  ←信号─  asyncio 事件循环
                                              ViewerSession（收帧/解密/还原）

Qt 与 asyncio 分居两个线程：asyncio 跑在后台线程，用 Qt 信号把画面送回主线程绘制。
信号跨线程时 Qt 会自动排队到主线程，所以绘制是安全的。

帧数据的所有权
--------------
``ViewerSession`` 在后台线程把画布**复制一份**再发出，主线程直接在这份内存上
构造 ``QImage``（不复制第二次），只要持有 numpy 数组的引用就安全。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import pathlib
import sys
import threading
import time

# 让 `python src/viewer/main.py` 能直接 import common
_SRC = pathlib.Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import config as config_mod          # noqa: E402
from common.console import setup_console         # noqa: E402

setup_console()

import numpy as np                               # noqa: E402
from PySide6.QtCore import Qt, QObject, QPoint, QRect, Signal, Slot  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (                  # noqa: E402
    QApplication, QLabel, QMainWindow, QMessageBox, QStatusBar, QWidget,
)

from viewer.session import ViewerSession         # noqa: E402

LOG = logging.getLogger("viewer")

# Qt 鼠标按键 -> 我们的协议编号（1=左 2=右 3=中）
QT_BUTTON_MAP = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2,
                 Qt.MouseButton.MiddleButton: 3}

# 这些 VK 是「扩展键」，注入时必须带 KEYEVENTF_EXTENDEDKEY，否则方向键等会失效
EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,   # PgUp PgDn End Home 方向键
    0x2D, 0x2E,                                        # Insert Delete
    0x5B, 0x5C, 0x5D,                                  # Win 键 / 右键菜单
    0x6F,                                              # 小键盘 /
    0xA3, 0xA5,                                        # 右 Ctrl / 右 Alt
}

_MAPVK_VK_TO_VSC = 0


def vk_to_scancode(vk: int) -> int:
    """Windows 虚拟键码 -> 扫描码。

    注入时用扫描码而不是字符，这样尊重**目标机器**的键盘布局
    （Viewer 和 Agent 的输入法/布局可以不同）。
    """
    if sys.platform != "win32" or vk <= 0:
        return vk
    try:
        sc = ctypes.windll.user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
        return sc or vk
    except Exception:
        return vk


class Bridge(QObject):
    """把后台 asyncio 线程的事件送回 Qt 主线程。"""
    frame_ready = Signal(object)
    status_ready = Signal(object)
    state_changed = Signal(str)
    clipboard_ready = Signal(str)
    log_line = Signal(str)


class ScreenWidget(QWidget):
    """画面显示区：负责绘制，以及把本地键鼠换算成归一化坐标。"""

    def __init__(self, bridge: Bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.session: ViewerSession | None = None
        self.scale_mode = "fit"

        self._frame: np.ndarray | None = None      # 持有引用，保证 QImage 内存有效
        self._qimage: QImage | None = None
        self._drawn = QRect()
        self._last_mouse = (0.0, 0.0)
        self._rendered = 0

        # 键鼠捕获需要焦点与鼠标追踪
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 200)
        self.setStyleSheet("background:#111;")

    # ---------------------------------------------------------- 帧

    @Slot(object)
    def on_frame(self, arr: np.ndarray) -> None:
        # arr 是后台线程已复制过的自有内存，这里直接引用即可，不必再复制一次
        self._frame = arr
        h, w = arr.shape[:2]
        self._qimage = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        self._rendered += 1
        if self._rendered == 1:
            LOG.info("[界面] 首帧已渲染 %dx%d，开始绘制", w, h)
        self.update()

    def has_frame(self) -> bool:
        return self._qimage is not None

    def frame_size(self):
        return (self._frame.shape[1], self._frame.shape[0]) if self._frame is not None else None

    # ---------------------------------------------------------- 绘制

    def _target_rect(self) -> QRect:
        if self._qimage is None:
            return QRect()
        iw, ih = self._qimage.width(), self._qimage.height()
        if iw <= 0 or ih <= 0:
            return QRect()
        if self.scale_mode == "actual":
            return QRect(0, 0, iw, ih)
        if self.scale_mode == "stretch":
            return self.rect()
        # fit：等比缩放到窗口内，居中
        s = min(self.width() / float(iw), self.height() / float(ih))
        w, h = max(1, int(iw * s)), max(1, int(ih * s))
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._qimage is None:
            p.setPen(Qt.GlobalColor.gray)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "等待画面……")
            return
        rect = self._target_rect()
        self._drawn = rect
        p.drawImage(rect, self._qimage)

    # ---------------------------------------------------------- 坐标换算

    def _normalized(self, pos: QPoint):
        r = self._drawn
        if r.width() <= 0 or r.height() <= 0:
            return 0.0, 0.0
        # 归一化 0~1：Agent 端无论是否降采样都能正确还原
        x = (pos.x() - r.x()) / float(r.width())
        y = (pos.y() - r.y()) / float(r.height())
        return min(1.0, max(0.0, x)), min(1.0, max(0.0, y))

    # ---------------------------------------------------------- 键鼠

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self.session:
            x, y = self._normalized(e.position().toPoint())
            self._last_mouse = (x, y)
            self.session.send_mouse_move(x, y)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self.session and e.button() in QT_BUTTON_MAP:
            x, y = self._normalized(e.position().toPoint())
            self._last_mouse = (x, y)
            self.session.send_mouse_button(QT_BUTTON_MAP[e.button()], True, x, y)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self.session and e.button() in QT_BUTTON_MAP:
            x, y = self._normalized(e.position().toPoint())
            self.session.send_mouse_button(QT_BUTTON_MAP[e.button()], False, x, y)

    def wheelEvent(self, e: QWheelEvent) -> None:
        if self.session:
            x, y = self._normalized(e.position().toPoint())
            # Qt 的 angleDelta 是 1/8 度；Windows 滚轮一格是 120
            self.session.send_wheel(int(e.angleDelta().y()), x, y)

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.isAutoRepeat():
            return
        if self.session:
            vk = e.nativeVirtualKey()
            sc = vk_to_scancode(vk)
            self.session.send_key(sc, True, vk in EXTENDED_VKS)

    def keyReleaseEvent(self, e: QKeyEvent) -> None:
        if e.isAutoRepeat():
            return
        if self.session:
            vk = e.nativeVirtualKey()
            sc = vk_to_scancode(vk)
            self.session.send_key(sc, False, vk in EXTENDED_VKS)


class ViewerWindow(QMainWindow):
    def __init__(self, cfg: config_mod.AppConfig, enable_input: bool, log=print):
        super().__init__()
        self.cfg = cfg
        self.enable_input = enable_input
        self.log = log
        self.setWindowTitle("ALSPD-DESK  ——  远程画面")
        self.resize(cfg.viewer.window_width, cfg.viewer.window_height)

        self.bridge = Bridge()
        self.screen = ScreenWidget(self.bridge)
        self.screen.scale_mode = cfg.viewer.scale_mode
        self.setCentralWidget(self.screen)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("正在连接……")
        self.status.addWidget(self.status_label)
        self.input_label = QLabel()
        self.status.addPermanentWidget(self.input_label)
        self._update_input_label()

        self.session = ViewerSession(
            cfg,
            # 注意：on_frame 会传 (画布, 元信息) 两个参数，而 Qt 信号只接受一个。
            # 必须在这里适配，否则 emit 会抛 TypeError 且**画面一帧都显示不出来**。
            on_frame=lambda arr, meta: self.bridge.frame_ready.emit(arr),
            on_status=self.bridge.status_ready.emit,
            on_state=self.bridge.state_changed.emit,
            on_clipboard=self.bridge.clipboard_ready.emit,
            log=log,
            enable_input=enable_input,
        )
        self.screen.session = self.session

        self.bridge.frame_ready.connect(self.screen.on_frame)
        self.bridge.status_ready.connect(self.on_status)
        self.bridge.state_changed.connect(self.on_state)
        self.bridge.clipboard_ready.connect(self.on_remote_clipboard)

        # ---- 剪贴板同步 ----
        self._last_clipboard = ""
        self._clipboard_sent = 0
        self._clipboard_applied = 0
        if cfg.viewer.clipboard_sync:
            try:
                QApplication.clipboard().dataChanged.connect(self.on_local_clipboard_changed)
                self._last_clipboard = QApplication.clipboard().text() or ""
            except Exception as e:
                LOG.warning("剪贴板监听挂载失败：%s", e)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- 后台线程

    def start_background(self) -> None:
        def runner():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.session.run())
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=runner, name="viewer-net", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------- 信号槽

    @Slot(object)
    def on_status(self, st: dict) -> None:
        size = st.get("size")
        size_txt = "%dx%d" % size if size else "?"
        self.status_label.setText(
            "画面 %s  |  %.1f fps  |  %.2f Mbps  |  解码 %.1f ms  |  RTT %.0f ms  |  帧 %d（关键帧 %d）"
            % (size_txt, st.get("fps", 0), st.get("mbps", 0), st.get("decode_ms", 0),
               st.get("rtt_ms", 0), st.get("total_frames", 0), st.get("keyframes", 0)))

    @Slot(str)
    def on_state(self, name: str) -> None:
        text = {
            "connecting": "正在连接中继……",
            "connected": "已连接",
            "reconnecting": "连接断开，正在重连……",
            "failed": "连接失败，稍后重试……",
            "stopped": "已停止",
        }.get(name, name)
        if name not in ("connected",):
            self.status_label.setText(text)

    def _update_input_label(self) -> None:
        if self.enable_input:
            self.input_label.setText("⚠️ 键鼠注入：已启用")
            self.input_label.setStyleSheet("color:#c00; font-weight:bold;")
        else:
            self.input_label.setText("✅ 键鼠注入：已禁用（只读观看）")
            self.input_label.setStyleSheet("color:#080;")

    # ---------------------------------------------------------- 剪贴板

    @Slot(str)
    def on_remote_clipboard(self, text: str) -> None:
        """把对端同步过来的剪贴板内容写到本机。"""
        if not self.cfg.viewer.clipboard_sync or not text:
            return
        try:
            cb = QApplication.clipboard()
            # 关键顺序：**先记录再写入**。
            # setText() 会在 Windows 上**同步**触发 dataChanged，
            # 如果先写后记，本地处理函数就会把刚写进来的内容当成「本地变化」
            # 又发回对端，形成无限回环。
            self._last_clipboard = text
            cb.setText(text)
            self._clipboard_applied += 1
            LOG.info("[剪贴板] 已应用对端内容（%d 字符）", len(text))
        except Exception as e:
            LOG.warning("写入本地剪贴板失败：%s", e)

    @Slot()
    def on_local_clipboard_changed(self) -> None:
        """本机剪贴板变化 -> 同步给对端。"""
        if not self.cfg.viewer.clipboard_sync:
            return
        try:
            text = QApplication.clipboard().text() or ""
        except Exception:
            return
        if not text or text == self._last_clipboard:
            return
        self._last_clipboard = text
        self._clipboard_sent += 1
        LOG.info("[剪贴板] 已发送本机内容（%d 字符）", len(text))
        self.session.send_clipboard(text)

    # ---------------------------------------------------------- 关闭

    def closeEvent(self, event) -> None:
        try:
            self.session.send_bye()
            self.session.stop()
        except Exception:
            pass
        super().closeEvent(event)


def build_logger(cfg: config_mod.AppConfig, quiet: bool = False) -> None:
    """配置日志。

    打包成 GUI 程序（PyInstaller --windowed）时 ``sys.stdout`` 是 ``None``，
    控制台输出全部消失、出错时完全看不到原因。所以：
    * 冻结运行时**默认写日志文件**（放在 exe 同目录）
    * 只在 stdout 真的存在时才加控制台 handler
    * 安装 excepthook，把未捕获异常也写进日志
    """
    level = logging.DEBUG if cfg.logging.level == "DEBUG" else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", "%Y-%m-%d %H:%M:%S")

    log_path = cfg.logging.file
    if not log_path and getattr(sys, "frozen", False):
        # 冻结运行时默认落盘 —— 否则 GUI 程序出问题将无从排查
        try:
            log_path = str(pathlib.Path(sys.executable).parent / "viewer.log")
        except Exception:
            log_path = None

    if log_path:
        try:
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass

    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if log_path:
        LOG.info("日志文件：%s", log_path)

    def _excepthook(exc_type, exc, tb):
        LOG.critical("未捕获的异常", exc_info=(exc_type, exc, tb))
        if not getattr(sys, "frozen", False):
            sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook


def run_selftest(cfg: config_mod.AppConfig) -> int:
    """bundle 自检：验证打包出来的 Viewer exe 在这台机器上能否正常起界面。"""
    problems = 0
    print("=" * 74)
    print("  Viewer 自检（不联网、不连接中继）")
    print("=" * 74)

    print("[1] 依赖模块")
    import numpy as np_
    print("    ✅ %-22s %s" % ("numpy", np_.__version__))
    try:
        import PySide6
        print("    ✅ %-22s %s" % ("PySide6", PySide6.__version__))
    except ImportError as e:
        print("    ❌ %-22s 缺失：%s" % ("PySide6", e))
        problems += 1
    for mod_name, label in (("PIL", "pillow"), ("websockets", "websockets"),
                            ("cryptography", "cryptography")):
        try:
            mod = __import__(mod_name)
            print("    ✅ %-22s %s" % (label, getattr(mod, "__version__", "")))
        except ImportError as e:
            print("    ❌ %-22s 缺失：%s" % (label, e))
            problems += 1

    print()
    print("[2] Qt 平台插件（打包最常见的失败点）")
    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        screen = app.primaryScreen()
        if screen:
            g = screen.geometry()
            print("    ✅ Qt 平台插件加载成功，主屏 %dx%d" % (g.width(), g.height()))
        else:
            print("    ⚠️  Qt 起来了但拿不到屏幕信息")
    except Exception as e:
        print("    ❌ Qt 初始化失败：%s: %s" % (type(e).__name__, e))
        print("       打包版常见原因：platforms 插件未被打进 exe。")
        problems += 1

    print()
    print("[3] 配置")
    probs = config_mod.validate_common(cfg)
    if probs:
        for p in probs:
            print("    ❌ %s" % p)
            problems += 1
    else:
        print("    ✅ relay_host / room / relay_token / password 均已填写")
    print("    配置文件    : %s" % (cfg.path or "(未找到)"))
    print("    显示缩放    : %s" % cfg.viewer.scale_mode)

    print()
    print("=" * 74)
    if problems == 0:
        print("  ✅ 自检通过（%d 个问题）" % problems)
        print("=" * 74)
        return 0
    print("  ❌ 自检发现 %d 个问题，见上文" % problems)
    print("=" * 74)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ALSPD-DESK 控制端（Viewer）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--scale", choices=["fit", "actual", "stretch"], default=None,
                    help="显示缩放模式，覆盖配置")
    ap.add_argument("--enable-input", action="store_true",
                    help="⚠️ 启用键鼠注入。默认禁用，只读观看")
    ap.add_argument("--no-clipboard", action="store_true",
                    help="禁用剪贴板同步（剪贴板可能含敏感内容）")
    ap.add_argument("--check", action="store_true", help="只校验配置后退出")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：验证依赖、Qt 平台插件、配置（不联网）")
    args = ap.parse_args()

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print("配置错误：%s" % e)
        return 2
    if args.scale:
        cfg.viewer.scale_mode = args.scale
    if args.no_clipboard:
        cfg.viewer.clipboard_sync = False

    if args.selftest:
        return run_selftest(cfg)

    print()
    print("=" * 72)
    print("  ALSPD-DESK  控制端 (Viewer)")
    print("=" * 72)
    print("  配置文件    : %s" % (cfg.path or "(未找到，使用默认值)"))
    print("  中继        : %s  端口 %s"
          % (cfg.common.relay_host or "(未配置)",
             ", ".join(str(p) for p in cfg.common.relay_ports)))
    print("  房间        : %s" % (cfg.common.room or "(未配置)"))
    if args.enable_input:
        print("  键鼠注入    : ⚠️  已启用")
    else:
        print("  键鼠注入    : ✅ 已禁用（只读观看；加 --enable-input 可开启）")
    print("  剪贴板同步  : %s"
          % ("✅ 已启用（纯文本，双向）" if cfg.viewer.clipboard_sync
             else "已禁用"))

    problems = config_mod.validate_common(cfg)
    if problems:
        for p in problems:
            print("  ❌ %s" % p)
        print("=" * 72)
        return 2
    print("=" * 72)
    print()

    if args.check:
        print("  配置校验通过（--check 模式，未启动界面）")
        return 0

    build_logger(cfg, quiet=False)

    app = QApplication(sys.argv)
    win = ViewerWindow(cfg, enable_input=args.enable_input, log=lambda m: LOG.info(m))
    win.show()
    win.start_background()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
