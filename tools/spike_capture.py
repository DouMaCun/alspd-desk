#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spike_capture.py —— Phase 0b 性能验证工具（本机运行）

用途
----
在写正式 Agent 之前，实测「采集 → 分块差分 → atlas 编码」这条链路的真实性能，
用来决定压缩参数（分辨率 / 分块大小 / JPEG 质量 / 帧率上限），
并判断 3~5 Mbps 的 VPS 带宽够不够用。

采集后端
--------
优先 dxcam（DXGI Desktop Duplication），失败自动回落 mss（GDI）。实测差距很大：
    dxcam 有新帧 4.65 ms ／ 静止时 0.11 ms
    mss          33 ms（无论画面是否变化都要付这个代价）
dxcam 失败的情形：Desktop Duplication 每个显示器只允许一个复制器（被 OBS/录屏占用时），
以及 RDP 远程会话下不支持。此时会自动回落 mss，不影响验证结果。

隐私
----
本工具 **只统计数字，不保存任何图像**。全程不向磁盘写入任何画面数据，
也不读取任何文件内容。

用法
----
    # 默认：主屏，跑 30 秒
    python tools/spike_capture.py --seconds 30

    # 指定显示器（见启动时打印的输出列表）
    python tools/spike_capture.py --output 1

    # 强制使用 mss 采集做对比
    python tools/spike_capture.py --capture mss

    # 缩放到「家里显示器/窗口」的宽度（关键：只发能显示的分辨率，画质无损）
    python tools/spike_capture.py --target-width 1920
    python tools/spike_capture.py --target-width 1280

运行期间建议依次做：
    前 1/3 秒数 —— 手离开键鼠（静止）
    中 1/3 秒数 —— 打字 / 小幅移动鼠标（办公）
    后 1/3 秒数 —— 快速滚动长网页 / 拖动窗口（动态）
"""

import argparse
import gc
import io
import statistics
import sys
import time

import numpy as np


def setup_console():
    """Windows 控制台默认 GBK，中文符号会 UnicodeEncodeError。统一切到 UTF-8。"""
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


setup_console()

try:
    from PIL import Image
except ImportError:
    print("缺少 pillow，请先安装：pip install pillow")
    sys.exit(1)

_RESAMPLE = getattr(Image, "Resampling", Image)


# ============================================================ 采集后端

class MSSCapturer:
    """GDI 采集（回落方案）。每次 grab 都返回新帧。"""

    name = "mss (GDI)"

    @staticmethod
    def _new_session(mss):
        """新版 mss 推荐 MSS()，旧版只有 mss()，做兼容。"""
        factory = getattr(mss, "MSS", None) or mss.mss
        return factory()

    def __init__(self, output_idx=None):
        import mss
        self._mss = mss
        self._sct = self._new_session(mss)
        # mss.monitors[0] 是全部显示器合并的虚拟屏，1..n 才是各显示器
        mons = self._sct.monitors
        if output_idx is not None:
            idx = output_idx + 1
            if idx >= len(mons):
                raise RuntimeError("显示器序号 %s 不存在（可用 0..%d）" % (output_idx, len(mons) - 2))
            self._mon = mons[idx]
        else:
            # 找主显示器；mss 的 monitors[1] 不一定是主屏，必须看 is_primary
            self._mon = mons[1]
            for m in mons[1:]:
                if m.get("is_primary"):
                    self._mon = m
                    break
        self.width = self._mon["width"]
        self.height = self._mon["height"]

    def grab(self):
        shot = self._sct.grab(self._mon)
        raw = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        # BGRA -> RGB，用 PIL 的 rawmode 在 C 层完成，比 numpy 花式索引快
        return np.asarray(Image.frombytes(
            "RGB", (shot.width, shot.height), raw.tobytes(), "raw", "BGRX"))

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


class DXCamCapturer:
    """DXGI Desktop Duplication 采集（首选）。画面无变化时 grab() 返回 None。"""

    name = "dxcam (DXGI)"

    def __init__(self, output_idx=None):
        import dxcam
        self._dxcam = dxcam
        # processor_backend="numpy" 可绕开对 opencv(cv2) 的依赖
        self._cam = dxcam.create(
            output_idx=output_idx,
            output_color="RGB",
            processor_backend="numpy",
        )
        self.width = self._cam.width
        self.height = self._cam.height

    def grab(self):
        frame = self._cam.grab()      # 无新帧时返回 None
        if frame is None:
            return None
        return frame

    def close(self):
        try:
            self._cam.stop()
        except Exception:
            pass
        try:
            del self._cam
        except Exception:
            pass
        gc.collect()


def list_outputs():
    """打印可用的显示器/输出，便于用户选 --output。"""
    print("  可用显示器：")
    try:
        import dxcam
        raw = dxcam.output_info()
        # output_info() 返回的可能是字符串，也可能是一组对象，统一处理
        text = raw if isinstance(raw, str) else "\n".join(str(x) for x in raw)
        for line in text.splitlines():
            if line.strip():
                print("    [dxcam] %s" % line.strip())
    except Exception as e:
        print("    [dxcam] 不可用：%s" % e)
    try:
        import mss
        with mss.MSS() as s:
            for i, m in enumerate(s.monitors[1:]):
                print("    [mss  ] #%d  %sx%s @ %s,%s  primary=%s"
                      % (i, m["width"], m["height"], m["left"], m["top"], m.get("is_primary")))
    except Exception as e:
        print("    [mss] 不可用：%s" % e)


def make_capturer(prefer, output_idx):
    """按偏好创建采集器，dxcam 失败自动回落 mss。"""
    errors = []
    if prefer in ("auto", "dxcam"):
        try:
            cap = DXCamCapturer(output_idx)
            note = "" if prefer == "dxcam" else "（自动选择）"
            print("  采集后端    : %s  %sx%s %s" % (cap.name, cap.width, cap.height, note))
            return cap
        except Exception as e:
            errors.append("dxcam: %s: %s" % (type(e).__name__, e))
            if prefer == "dxcam":
                print("  ⚠️  dxcam 初始化失败：%s" % e)
    try:
        cap = MSSCapturer(output_idx)
        print("  采集后端    : %s  %sx%s" % (cap.name, cap.width, cap.height))
        for err in errors:
            print("                （dxcam 回落原因：%s）" % err)
        return cap
    except Exception as e:
        errors.append("mss: %s: %s" % (type(e).__name__, e))
    print("  ❌ 所有采集后端都不可用：")
    for err in errors:
        print("      %s" % err)
    return None


# ============================================================ 差分

def tile_diff(prev, cur, tile_size, threshold):
    """按 tile_size 切块差分，返回 (变化块坐标列表, 变化占比, 行数, 列数)。

    性能关键：把颜色通道并入 tile 归约（max(axis=(1,3,4))），
    比常见的 `d.max(axis=2)` 后再归约快约 7.7 倍（原生 2560x1408：118ms -> 15.4ms）。
    这是 numpy 对小尾轴归约开销极高的经典陷阱。
    """
    h, w = cur.shape[:2]
    th, tw = h // tile_size, w // tile_size
    if th == 0 or tw == 0:
        return [], 0.0, 0, 0
    ph, pw = th * tile_size, tw * tile_size

    a = cur[:ph, :pw].reshape(th, tile_size, tw, tile_size, 3)
    b = prev[:ph, :pw].reshape(th, tile_size, tw, tile_size, 3)
    d = np.maximum(a, b) - np.minimum(a, b)        # uint8 差值，无需转 int16
    per_tile = d.max(axis=(1, 3, 4))                # (th, tw)

    ys, xs = np.nonzero(per_tile > threshold)
    coords = list(zip(ys.tolist(), xs.tolist()))
    return coords, (len(coords) / float(th * tw) if th * tw else 0.0), th, tw


def build_atlas(img, coords, tile_size):
    """把变化块拼成一张 atlas（只编码一次），比逐块编码又快又小。

    实测 120 块：atlas 44.2KB/3.6ms  vs  逐块 117KB/7.6ms
    """
    n = len(coords)
    if n == 0:
        return None
    cols = int(np.ceil(np.sqrt(n)))
    rows = int(np.ceil(n / cols))
    atlas = Image.new("RGB", (cols * tile_size, rows * tile_size))
    for i, (ty, tx) in enumerate(coords):
        box = (tx * tile_size, ty * tile_size, (tx + 1) * tile_size, (ty + 1) * tile_size)
        atlas.paste(img.crop(box), ((i % cols) * tile_size, (i // cols) * tile_size))
    return atlas


# ============================================================ 编码

def _save(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def encode_pil(img, method):
    if method == "jpeg_q45":
        return _save(img, "JPEG", quality=45, subsampling=2)
    if method == "jpeg_q60":
        return _save(img, "JPEG", quality=60, subsampling=2)
    if method == "jpeg_q75":
        return _save(img, "JPEG", quality=75, subsampling=2)
    if method == "jpeg_q60_444":
        return _save(img, "JPEG", quality=60, subsampling=0)
    if method == "jpeg_q80_444":
        return _save(img, "JPEG", quality=80, subsampling=0)
    raise ValueError("未知编码方式: %s" % method)


FULL_METHODS = ["jpeg_q45", "jpeg_q60", "jpeg_q75", "jpeg_q60_444", "jpeg_q80_444"]
TILE_METHODS = ["jpeg_q60", "jpeg_q75"]


def decode_ms(data):
    t0 = time.perf_counter()
    im = Image.open(io.BytesIO(data))
    im.load()
    return (time.perf_counter() - t0) * 1000.0


# ============================================================ 统计小工具

def pct(values, p):
    if not values:
        return 0.0
    return float(np.percentile(values, p))


def tail_mean(values, n):
    if not values or n <= 0:
        return 0.0
    tail = values[-n:]
    return statistics.mean(tail) if tail else 0.0


def downsample(img, target_w):
    """缩放到目标宽度。

    关键设计：目标宽度应当等于「Viewer 窗口的显示宽度」—— 只发送能在家里那台
    真正显示出来的分辨率，画质上无损，带宽却省数倍。而不是无脑降质。

    整数分之一走 reduce()（快速盒式滤波，质量好），否则走 resize BOX。
    """
    if target_w is None or target_w >= img.width:
        return img, "不缩放"
    factor = img.width / float(target_w)
    f_int = int(round(factor))
    if f_int >= 2 and abs(factor - f_int) < 1e-9 \
            and img.width % f_int == 0 and img.height % f_int == 0:
        return img.reduce(f_int), "reduce(%d) 盒式" % f_int
    h = max(1, int(round(img.height * target_w / float(img.width))))
    return img.resize((target_w, h), _RESAMPLE.BOX), "resize BOX"


# ============================================================ 主流程

def main():
    ap = argparse.ArgumentParser(description="ALSPD-DESK Phase 0b 屏幕采集性能验证")
    ap.add_argument("--seconds", type=float, default=30.0, help="采样时长（秒），默认 30")
    ap.add_argument("--capture", choices=["auto", "dxcam", "mss"], default="auto",
                    help="采集后端。默认 auto（dxcam 优先，失败回落 mss）")
    ap.add_argument("--output", type=int, default=None,
                    help="显示器序号，默认主屏。不带参数运行可看到可用列表")
    ap.add_argument("--target-width", type=int, default=0,
                    help="缩放到该宽度，0=原生（默认）。建议填家里 Viewer 窗口的宽度，"
                         "只发能显示的分辨率，画质无损且省带宽")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="缩放比例（--target-width 优先）。1.0=原生，0.5=缩一半")
    ap.add_argument("--tile", type=int, default=64, help="分块边长（像素），默认 64")
    ap.add_argument("--threshold", type=int, default=8,
                    help="判定「变化」的像素差阈值 0~255，默认 8")
    ap.add_argument("--max-fps", type=float, default=30.0, help="采集循环帧率上限，默认 30")
    ap.add_argument("--list", action="store_true", help="只列出可用显示器然后退出")
    args = ap.parse_args()

    print()
    print("=" * 78)
    print("  ALSPD-DESK  Phase 0b  屏幕采集性能验证")
    print("=" * 78)

    if args.list:
        list_outputs()
        return 0

    print("  ⚠️  本工具只统计数字，不保存任何图像，不写入任何画面数据到磁盘。")
    print()

    cap = make_capturer(args.capture, args.output)
    if cap is None:
        return 1

    TS = args.tile
    src_w, src_h = cap.width, cap.height
    # 必须对齐到分块的整数倍，否则 reshape 会失败（1440 不是 64 的倍数）
    W = (src_w // TS) * TS
    H = (src_h // TS) * TS
    TH, TW = H // TS, W // TS

    # 目标宽度：--target-width 优先，其次 --scale。默认原生，不做任何缩放
    if args.target_width and args.target_width > 0:
        target_w = args.target_width
    elif args.scale < 1.0:
        target_w = int(round(W * args.scale))
    else:
        target_w = None
    if target_w is not None:
        target_w = (target_w // TS) * TS          # 对齐到分块整数倍
        if target_w >= W:
            print("  ⚠️  目标宽度 %s 不小于源宽度 %s，按不缩放处理" % (target_w, W))
            target_w = None
        elif target_w < TS:
            target_w = TS

    print("  源分辨率    : %sx%s  ->  对齐后 %sx%s（块 %dx%d = %d 块）"
          % (src_w, src_h, W, H, TH, TW, TH * TW))
    if target_w is None:
        print("  目标宽度    : 原生（不缩放，文字最清晰）")
    else:
        th_est = ((int(round(H * target_w / float(W)))) // TS) * TS
        print("  目标宽度    : %s  ->  约 %sx%s" % (target_w, target_w, th_est))
    print("  变化阈值    : %s" % args.threshold)
    print("  采样时长    : %ss  帧率上限 %s" % (args.seconds, args.max_fps))
    print()
    print("  期间请依次做：静止 → 打字 → 快速滚动，以覆盖三种场景")
    print("-" * 78)

    frame_interval = 1.0 / args.max_fps
    prev_small = None
    scale_label = "不缩放"

    cap_times, prep_times, diff_times, atlas_build_times, lazy_conv_times = [], [], [], [], []
    atlas_enc_times = {m: [] for m in TILE_METHODS}
    atlas_bytes = {m: [] for m in TILE_METHODS}
    indiv_bytes = {m: [] for m in TILE_METHODS}
    per_update_bytes = []           # 真实更新包（atlas+q60 + 坐标表）
    changed_ratios = []
    changed_counts = []
    polls = 0
    changed_frames = 0
    first_full_image = None

    sec_changed = 0
    sec_frames = 0
    sec_bytes = 0
    last_report = time.perf_counter()
    start = time.perf_counter()

    try:
        while True:
            now = time.perf_counter()
            if now - start >= args.seconds:
                break

            t0 = time.perf_counter()
            rgb = cap.grab()
            t1 = time.perf_counter()
            polls += 1

            if rgb is None:
                # dxcam：画面没变化。这正是它相对 mss 的最大优势 —— 静止时零开销
                time.sleep(0.002)
                if now - last_report >= 1.0:
                    print("  [%4.0fs] 帧 %2d  变化块    0 ( 0.0%%)  本秒     0.0 KB  (画面静止)"
                          % (now - start, sec_frames))
                    last_report = now
                    sec_changed = sec_frames = 0
                    sec_bytes = 0
                continue

            changed_frames += 1
            cap_times.append((t1 - t0) * 1000)

            # 准备差分用的小图。不缩放时直接用 numpy 视图，完全不碰 PIL ——
            # 省掉 Image.fromarray + np.asarray 的往返（实测约 15ms/帧）
            t2 = time.perf_counter()
            if target_w is None:
                small = rgb[:H, :W]
                if not small.flags["C_CONTIGUOUS"]:
                    small = np.ascontiguousarray(small)
                img_small = None
                scale_label = "不缩放"
            else:
                img_small, scale_label = downsample(Image.fromarray(rgb[:H, :W]), target_w)
                small = np.asarray(img_small)
                if first_full_image is None:
                    first_full_image = img_small
            t3 = time.perf_counter()
            prep_times.append((t3 - t2) * 1000)

            if prev_small is not None and prev_small.shape == small.shape:
                t4 = time.perf_counter()
                coords, ratio, th, tw = tile_diff(prev_small, small, TS, args.threshold)
                t5 = time.perf_counter()
                diff_times.append((t5 - t4) * 1000)
                changed_ratios.append(ratio)
                changed_counts.append(len(coords))
                sec_changed += len(coords)

                if coords:
                    t6 = time.perf_counter()
                    if img_small is None:
                        # 懒创建：只有真的检测到变化，才付 PIL 转换的代价
                        img_small = Image.fromarray(small)
                        if first_full_image is None:
                            first_full_image = img_small
                    t7 = time.perf_counter()
                    lazy_conv_times.append((t7 - t6) * 1000)

                    atlas = build_atlas(img_small, coords, TS)
                    t8 = time.perf_counter()
                    atlas_build_times.append((t8 - t7) * 1000)
                    if atlas is not None:
                        for m in TILE_METHODS:
                            ta = time.perf_counter()
                            data = encode_pil(atlas, m)
                            tb = time.perf_counter()
                            atlas_bytes[m].append(len(data))
                            atlas_enc_times[m].append((tb - ta) * 1000)
                            if m == "jpeg_q60":
                                indiv = 0
                                for (ty, tx) in coords:
                                    box = (tx * TS, ty * TS, (tx + 1) * TS, (ty + 1) * TS)
                                    indiv += len(encode_pil(img_small.crop(box), m))
                                indiv_bytes[m].append(indiv)
                                # 真实更新包 = atlas + 坐标表（每块 4 个 uint16 = 8 字节）+ 包头
                                pkt = len(data) + len(coords) * 8 + 16
                                per_update_bytes.append(pkt)
                                sec_bytes += pkt

            prev_small = small
            sec_frames += 1

            if now - last_report >= 1.0:
                print("  [%4.0fs] 帧 %2d  变化块 %4d (%4.1f%%)  本秒 %7.1f KB  采集%5.1fms 预处理%5.1fms 差分%5.1fms"
                      % (now - start, sec_frames, sec_changed,
                         tail_mean(changed_ratios, sec_frames) * 100,
                         sec_bytes / 1024.0,
                         tail_mean(cap_times, sec_frames),
                         tail_mean(prep_times, sec_frames),
                         tail_mean(diff_times, sec_frames)))
                last_report = now
                sec_changed = sec_frames = 0
                sec_bytes = 0

            elapsed = time.perf_counter() - now
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

    except KeyboardInterrupt:
        print("\n  收到 Ctrl-C，提前结束采样")
    finally:
        cap.close()

    wall = time.perf_counter() - start
    print("-" * 78)
    print("  采样完成：轮询 %d 次，其中画面有变化 %d 次，用时 %.1f 秒"
          % (polls, changed_frames, wall))
    if changed_frames == 0:
        print("  ⚠️  采样期间画面完全没变化，无法得出带宽结论。")
        print("      请重跑，并在期间打字或滚动网页。")
    print()

    # ---------------------------------------------------- 各环节耗时
    print("=" * 78)
    print("  【1】链路各环节耗时（毫秒）")
    print("=" * 78)
    print("  %-18s %9s %9s %9s %8s" % ("环节", "中位", "P90", "最大", "样本"))
    print("  " + "-" * 58)
    for name, arr in (("采集(有变化帧)", cap_times), ("预处理(转PIL/缩放)", prep_times),
                      ("分块差分", diff_times), ("PIL懒转换", lazy_conv_times),
                      ("atlas 拼接", atlas_build_times)):
        if arr:
            print("  %-18s %9.2f %9.2f %9.2f %8d"
                  % (name, pct(arr, 50), pct(arr, 90), max(arr), len(arr)))
    enc = atlas_enc_times.get("jpeg_q60") or []
    if enc:
        print("  %-18s %9.2f %9.2f %9.2f %8d"
              % ("atlas JPEG q60", pct(enc, 50), pct(enc, 90), max(enc), len(enc)))
    pipeline = (pct(cap_times, 50) + pct(prep_times, 50) + pct(diff_times, 50)
                + pct(lazy_conv_times, 50) + pct(atlas_build_times, 50) + pct(enc, 50))
    print("  " + "-" * 58)
    print("  %-18s %9.2f   -> 单核理论上限约 %.0f fps" % ("合计", pipeline, 1000.0 / max(pipeline, 0.01)))
    print()

    # ---------------------------------------------------- 更新包体积
    print("=" * 78)
    print("  【2】更新包体积（真实使用场景）")
    print("=" * 78)
    if per_update_bytes:
        print("  有更新的帧 %d / %d 帧" % (len(per_update_bytes), changed_frames))
        print("  变化块占比  中位 %.1f%%   P90 %.1f%%" % (pct(changed_ratios, 50) * 100, pct(changed_ratios, 90) * 100))
        print("  变化块数量  中位 %.0f    P90 %.0f    最大 %d"
              % (pct(changed_counts, 50), pct(changed_counts, 90), max(changed_counts)))
        print()
        print("  %-20s %10s %10s %10s" % ("更新包", "中位", "P90", "最大"))
        print("  " + "-" * 54)
        print("  %-20s %8.1fKB %8.1fKB %8.1fKB"
              % ("atlas+坐标表", pct(per_update_bytes, 50) / 1024,
                 pct(per_update_bytes, 90) / 1024, max(per_update_bytes) / 1024))
        print()
        print("  按 P90 估算带宽需求：")
        print("  %-8s %14s %14s" % ("帧率", "按中位", "按P90(稳妥)"))
        print("  " + "-" * 40)
        for fps in (5, 10, 15, 20, 25):
            med = pct(per_update_bytes, 50) * 8 * fps / 1e6
            p90 = pct(per_update_bytes, 90) * 8 * fps / 1e6
            if p90 <= 3.0:
                flag = "  ✅ 3Mbps 内"
            elif p90 <= 5.0:
                flag = "  ⚠️ 需 5Mbps"
            else:
                flag = "  ❌ 超 5Mbps"
            print("  %-8s %11.2f Mbps %11.2f Mbps%s" % ("%dfps" % fps, med, p90, flag))
        print()
        for budget, label in ((3.0, "3 Mbps"), (5.0, "5 Mbps")):
            p90b = pct(per_update_bytes, 90) * 8
            if p90b > 0:
                print("  在 %s 下（按 P90）：可持续约 %.1f fps" % (label, budget * 1e6 / p90b))
        print()
    else:
        print("  没有测到更新包（采样期间画面没变化）。")
        print()

    # ---------------------------------------------------- atlas vs 逐块
    if indiv_bytes.get("jpeg_q60"):
        print("=" * 78)
        print("  【3】编码方案对照：单张 atlas vs 逐块编码（同样内容）")
        print("=" * 78)
        print("  %-12s %12s %12s %10s" % ("方式", "中位体积", "编码耗时", "说明"))
        print("  " + "-" * 50)
        print("  %-12s %10.1fKB %10.2fms   一次编码"
              % ("atlas", pct(atlas_bytes["jpeg_q60"], 50) / 1024, pct(atlas_enc_times["jpeg_q60"], 50)))
        print("  %-12s %10.1fKB %10s   每块编码一次"
              % ("逐块", pct(indiv_bytes["jpeg_q60"], 50) / 1024, "-"))
        saved = (1 - pct(atlas_bytes["jpeg_q60"], 50) / max(pct(indiv_bytes["jpeg_q60"], 50), 1)) * 100
        print()
        print("  -> atlas 比逐块省 %.0f%% 体积，且只需一次编码调用" % saved)
        print()

    # ---------------------------------------------------- 整帧对照
    print("=" * 78)
    print("  【4】整帧编码对照（最坏情况：全屏都在变化）")
    print("=" * 78)
    if first_full_image is None and prev_small is not None:
        first_full_image = Image.fromarray(prev_small)
    if first_full_image is not None:
        print("  基准图：真实屏幕内容 %sx%s%s"
              % (first_full_image.width, first_full_image.height,
                 "" if target_w is None else "（%s）" % scale_label))
        print()
        print("  %-14s %11s %10s %10s %12s" % ("编码方式", "体积", "编码ms", "解码ms", "10fps 需"))
        print("  " + "-" * 62)
        for m in FULL_METHODS:
            try:
                t0 = time.perf_counter()
                data = encode_pil(first_full_image, m)
                t1 = time.perf_counter()
                dec = decode_ms(data)
                enc_ms = (t1 - t0) * 1000
                print("  %-14s %9.1fKB %10.2f %10.2f %9.2f Mbps"
                      % (m, len(data) / 1024, enc_ms, dec, len(data) * 8 * 10 / 1e6))
            except Exception as e:
                print("  %-14s 失败: %s" % (m, e))
    print()

    # ---------------------------------------------------- 结论
    print("=" * 78)
    print("  【5】结论与参数建议")
    print("=" * 78)
    print("  采集后端    : %s" % cap.name)
    print("  源分辨率    : %sx%s" % (src_w, src_h))
    if target_w is None:
        print("  发送分辨率  : %sx%s（原生，未缩放）" % (W, H))
    else:
        print("  发送分辨率  : %sx%s（目标宽度 %s，%s）"
              % (first_full_image.width if first_full_image else "?",
                 first_full_image.height if first_full_image else "?",
                 target_w, scale_label))
    print("  分块大小    : %dx%d" % (TS, TS))
    if per_update_bytes:
        p90b = pct(per_update_bytes, 90) * 8
        best_fps = min(20, max(5, int(5.0 * 1e6 / p90b))) if p90b > 0 else 20
        print("  建议帧率上限: %d fps" % best_fps)
        print("  建议编码    : atlas 拼接 + 单次 JPEG q60")
        print("  单帧预算    : %.1f KB (P90)" % (pct(per_update_bytes, 90) / 1024))
    print()
    print("  把【2】的 P90 字节数 × 预期帧率，对照 VPS 带宽（3~5 Mbps）判断是否够用。")
    print("  参考【4】的整帧数据：全屏刷新时按当前分辨率需要多少带宽。")
    print("  落在预算内即可进入 Phase 1。")
    print("=" * 78)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
