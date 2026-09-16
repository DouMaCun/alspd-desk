# -*- coding: utf-8 -*-
"""屏幕采集：dxcam(DXGI) 优先，失败自动回落 mss(GDI)。

实测数据（2560×1440，本项目 Phase 0b）：

============  ============  ==============
后端           有新帧时       画面静止时
============  ============  ==============
dxcam (DXGI)   **4.65 ms**   **0.11 ms**
mss (GDI)      33 ms         33 ms（无论是否变化都要付）
============  ============  ==============

dxcam 在画面无变化时 ``grab()`` 返回 ``None`` —— 静止时几乎零开销，
这对办公场景（大部分时间画面静止）是决定性优势。

**为何必须保留 mss 回落**：DXGI Desktop Duplication 有两个硬限制
------------------------------------
1. **每个显示器只允许一个复制器** —— 被 OBS / 录屏软件占用时会初始化失败
2. **RDP 远程会话下不支持**

多显示器坐标
------------
键鼠注入需要显示器在**虚拟桌面**里的位置。但 dxcam 只暴露采集尺寸
（``region`` 是输出本地坐标，不含偏移），所以这里靠 **mss 的显示器枚举**
（它给出 left/top）来还原位置，见 ``resolve_monitor_rect``。
"""

from __future__ import annotations

import gc
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from .input import primary_screen_size


class CaptureError(Exception):
    """采集初始化或抓帧失败。"""


# DuplicateOutput 的 E_ACCESSDENIED（0x80070005）。这是最常见的一种失败，
# 而且原因很不直观，所以单独给一句可操作的提示。
_E_ACCESSDENIED = -2147024891


def _dxgi_hint(exc: Exception) -> str:
    """把 DXGI 的失败翻译成可操作的提示。"""
    code = None
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        code = args[0]
    if code == _E_ACCESSDENIED:
        return ("\n      原因通常是：**该显示器已被别的程序占用桌面复制器**"
                "（Windows 限制每个显示器同时只能有一个）。\n"
                "      常见占用者：OBS、录屏/截图工具、其他远程桌面客户端、"
                "Xbox Game Bar。\n"
                "      另一个可能：会话处于锁定状态或显示器已关闭。\n"
                "      排查：关掉上述程序后重试；也可先跑 --selftest 看是否恢复。\n"
                "      （不影响使用：会自动回落到 mss/GDI，只是采集慢约 7 倍）")
    return ""


# ---------------------------------------------------------------- 多显示器

def list_monitors() -> List[Dict]:
    """枚举显示器，带**虚拟桌面位置**。用 mss 枚举（它给出 left/top）。

    返回每项的 ``index`` 从 0 开始（对应 ``agent.output`` 这类配置）。
    失败返回空列表。
    """
    try:
        import mss
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as s:
            out = []
            # monitors[0] 是「所有显示器拼成的大桌面」，从 1 开始才是单块显示器
            for i, m in enumerate(s.monitors[1:]):
                out.append({
                    "index": i,
                    "left": int(m["left"]), "top": int(m["top"]),
                    "width": int(m["width"]), "height": int(m["height"]),
                    "primary": bool(m.get("is_primary")),
                    "name": m.get("name") or "",
                })
            return out
    except Exception:
        return []


def parse_dxcam_outputs(info: str) -> List[Dict]:
    """解析 ``dxcam.output_info()`` 的文本。

    这是在**分辨率相同、光看尺寸无法区分**时的兜底手段。
    dxcam 没有把这个信息暴露成结构化数据，只能解析它打印的字符串 ——
    所以这里写得比较防御：解析不出来就返回空列表，由调用方决定怎么办。
    """
    out: List[Dict] = []
    pattern = re.compile(
        r"Output\[(\d+)\].*?Res:\((\d+),\s*(\d+)\).*?Rot:(\d+).*?Primary:(True|False)")
    for line in str(info or "").splitlines():
        m = pattern.search(line)
        if m:
            out.append({
                "output": int(m.group(1)),
                "width": int(m.group(2)), "height": int(m.group(3)),
                "rotation": int(m.group(4)),
                "primary": m.group(5) == "True",
            })
    return out


def resolve_monitor_rect(output_idx: Optional[int], cam_w: int, cam_h: int,
                         monitors: Optional[List[Dict]] = None,
                         dxcam_outputs: Optional[List[Dict]] = None):
    """把「采集的是哪块屏」还原成虚拟桌面上的 ``(left, top, width, height)``。

    策略（从可靠到不可靠依次尝试）：

    1. **按分辨率唯一匹配** —— 最可靠，而且不依赖解析 dxcam 的调试字符串。
       本机就是这种情况：2560x1440 只有主屏，1920x1080 只有副屏。
    2. 分辨率有重复时，用 dxcam 的 ``Primary`` 标志区分（若调用方提供了）
    3. 都不行就返回 None —— 宁可不给，也不要给错。注入位置错了比不注入更糟。

    注意旋转：显示器旋转 90°/270° 时，枚举到的宽高与采集的宽高是互换的。
    """
    mons = monitors if monitors is not None else list_monitors()
    if not mons:
        return None

    candidates = [m for m in mons
                  if (m["width"] == cam_w and m["height"] == cam_h)
                  or (m["width"] == cam_h and m["height"] == cam_w)]
    if not candidates:
        return None

    if len(candidates) == 1:
        m = candidates[0]
        return (m["left"], m["top"], m["width"], m["height"])

    # 分辨率相同：借 dxcam 的 Primary 标志来区分
    outs = dxcam_outputs if dxcam_outputs is not None else []
    if output_idx is not None:
        info = next((o for o in outs if o.get("output") == output_idx), None)
        if info is not None:
            pick = [m for m in candidates if m["primary"] == info.get("primary")]
            if len(pick) == 1:
                return (pick[0]["left"], pick[0]["top"],
                        pick[0]["width"], pick[0]["height"])

    return None


def describe_monitors() -> str:
    """给用户看的显示器清单（用于自检与排错）。"""
    mons = list_monitors()
    if not mons:
        return "    （无法枚举显示器）"
    lines = []
    for m in mons:
        lines.append("    #%d  %dx%d @ %d,%d  %s%s"
                     % (m["index"], m["width"], m["height"], m["left"], m["top"],
                        "主屏" if m["primary"] else "副屏",
                        ("  %s" % m["name"]) if m["name"] else ""))
    return "\n".join(lines)


class Capturer:
    """采集器接口：抓一帧 RGB 图像，无新帧时返回 None。"""

    name = "?"
    width = 0
    height = 0

    def grab(self, force: bool = False) -> Optional[np.ndarray]:
        """抓一帧。

        ``force=True`` 时即使画面没有变化也要返回画面（用上次缓存的帧）。
        这是必要的：画面完全静止时 dxcam 永远返回 None，
        而新连上来的 Viewer 需要立刻看到当前画面（首个关键帧）。
        """
        raise NotImplementedError

    def close(self) -> None:
        pass

    def monitor_rect(self):
        """返回所采集显示器的 ``(left, top, width, height)``（虚拟桌面坐标）。

        键鼠注入需要它来把归一化坐标换算成绝对坐标。返回 ``None`` 表示
        无法确定 —— 宁可不给也不要给错，注入位置错了比不注入更糟。
        """
        return None


class DXCamCapturer(Capturer):
    """DXGI Desktop Duplication。画面无变化时返回 None。"""

    name = "dxcam (DXGI)"

    def __init__(self, output_idx: Optional[int] = None):
        try:
            import dxcam
        except ImportError as e:
            raise CaptureError("未安装 dxcam：pip install dxcam （%s）" % e) from None
        self._dxcam = dxcam
        self._output_idx = 0 if output_idx is None else output_idx
        try:
            # processor_backend="numpy" 可绕开对 opencv(cv2) 的依赖
            self._cam = dxcam.create(
                output_idx=output_idx,
                output_color="RGB",
                processor_backend="numpy",
            )
        except Exception as e:
            raise CaptureError("dxcam 初始化失败：%s: %s%s"
                               % (type(e).__name__, e, _dxgi_hint(e))) from None
        self.width = self._cam.width
        self.height = self._cam.height

    def monitor_rect(self):
        """所采集显示器在虚拟桌面上的位置。

        dxcam 不暴露偏移（``region`` 是输出本地坐标），所以靠 mss 的显示器枚举
        按尺寸还原；尺寸有重复时再用 dxcam 的 Primary 标志区分。
        """
        try:
            import dxcam
            outs = parse_dxcam_outputs(dxcam.output_info())
        except Exception:
            outs = []
        return resolve_monitor_rect(self._output_idx, self.width, self.height,
                                    dxcam_outputs=outs)

    def grab(self, force: bool = False) -> Optional[np.ndarray]:
        # copy=True（默认）保证返回的是自有内存，可以安全持有作为上一帧。
        # new_frame_only=False 会回退到上次缓存的帧 —— 静止时也能拿到画面，
        # 这是「首个关键帧」能发出去的前提。
        return self._cam.grab(new_frame_only=not force)

    def close(self) -> None:
        try:
            self._cam.stop()
        except Exception:
            pass
        try:
            del self._cam
        except Exception:
            pass
        gc.collect()


class MSSCapturer(Capturer):
    """GDI 采集（回落方案）。每次 grab 都返回新帧，从不返回 None。"""

    name = "mss (GDI)"

    def __init__(self, output_idx: Optional[int] = None):
        try:
            import mss
        except ImportError as e:
            raise CaptureError("未安装 mss：pip install mss （%s）" % e) from None
        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()
        mons = self._sct.monitors
        if output_idx is not None:
            idx = output_idx + 1
            if idx >= len(mons):
                raise CaptureError("显示器序号 %s 不存在（可用 0..%d）" % (output_idx, len(mons) - 2))
            self._mon = mons[idx]
        else:
            # mss 的 monitors[1] 不一定是主屏，必须看 is_primary
            self._mon = mons[1]
            for m in mons[1:]:
                if m.get("is_primary"):
                    self._mon = m
                    break
        self.width = self._mon["width"]
        self.height = self._mon["height"]
        self._pil = None

    def grab(self, force: bool = False) -> Optional[np.ndarray]:
        # GDI 每次都能拿到画面，force 无影响（忽略即可）
        shot = self._sct.grab(self._mon)
        raw = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        # BGRA -> RGB：用 PIL 的 rawmode 在 C 层完成，比 numpy 花式索引快
        if self._pil is None:
            from PIL import Image
            self._pil = Image
        return np.asarray(self._pil.frombytes(
            "RGB", (shot.width, shot.height), raw.tobytes(), "raw", "BGRX"))

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    def monitor_rect(self):
        # mss 直接给了显示器在虚拟桌面里的位置，最可靠
        m = self._mon
        return (int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]))


def make_capturer(backend: str = "auto", output_idx: Optional[int] = None, log=print) -> Capturer:
    """按偏好创建采集器。``backend`` 取 auto / dxcam / mss。"""
    errors = []
    if backend in ("auto", "dxcam"):
        try:
            cap = DXCamCapturer(output_idx)
            log("[采集] 使用 %s %dx%d" % (cap.name, cap.width, cap.height))
            return cap
        except CaptureError as e:
            errors.append(str(e))
            if backend == "dxcam":
                raise
            log("[采集] dxcam 不可用，回落 mss：%s" % e)

    try:
        cap = MSSCapturer(output_idx)
        log("[采集] 使用 %s %dx%d" % (cap.name, cap.width, cap.height))
        for err in errors:
            log("[采集] （回落原因：%s）" % err)
        return cap
    except CaptureError as e:
        errors.append(str(e))

    raise CaptureError("所有采集后端都不可用：\n  - " + "\n  - ".join(errors))
