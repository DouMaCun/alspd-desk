# -*- coding: utf-8 -*-
"""差分与编码：把新一帧压成一个「只含变化块」的更新包。

流水线
------
::

    原始帧(RGB numpy)
      → 与上一帧做 64×64 分块差分，找出变化块
      → 只把变化块拼成一张 atlas（一次 JPEG 编码）
      → 得到更新包（atlas + 坐标表）

关键优化（都经 Phase 0b 实测验证）
----------------------------------
1. **差分归约把颜色通道并入 tile 维**（``max(axis=(1,3,4))``），
   而不是先 ``d.max(axis=2)`` 再归约。后者是 numpy 对小尾轴归约的经典性能陷阱：
   原生 2560×1408 下 118 ms → **15.4 ms**，快 7.7 倍，检出结果完全一致。
2. **atlas 单次编码**比逐块编码**省 44% 体积**，且只需一次编码调用。
3. 差分在原始 numpy 上做，**不经过 PIL**；只有真有变化时才做 PIL 转换（懒创建）。
   省掉一次 ``Image.fromarray`` + ``np.asarray`` 往返（实测约 15 ms/帧）。
4. **降采样只允许整数倍**（``Image.reduce``）。非整数倍 resize 慢到会吃掉全部收益
   —— 实测 2560→1920 要 34.7 ms，比不缩放（29.7 ms）还慢。
"""

from __future__ import annotations

import io
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

_RESAMPLE = getattr(Image, "Resampling", Image)

# 变化块坐标：(源块行, 源块列, atlas 块列, atlas 块行)
Tile = Tuple[int, int, int, int]


class EncoderError(Exception):
    """编码失败。"""


def tile_diff(prev: np.ndarray, cur: np.ndarray, tile_size: int, threshold: int):
    """分块差分。

    返回 ``(变化块坐标列表, 变化占比, 块行数, 块列数)``。
    坐标是 ``(ty, tx)``，以块为单位。

    prev/cur 必须是同尺寸的 ``(h, w, 3)`` uint8 RGB。
    """
    if cur.ndim != 3 or cur.shape[2] != 3:
        raise EncoderError("期望 (h,w,3) 的 RGB 数组，实际 %s" % (cur.shape,))
    h, w = cur.shape[:2]
    th, tw = h // tile_size, w // tile_size
    if th == 0 or tw == 0:
        return [], 0.0, 0, 0

    # 裁剪到分块的整数倍：1440 不是 64 的倍数，不裁剪 reshape 会直接报错
    ph, pw = th * tile_size, tw * tile_size

    a = np.ascontiguousarray(cur[:ph, :pw]).reshape(th, tile_size, tw, tile_size, 3)
    b = np.ascontiguousarray(prev[:ph, :pw]).reshape(th, tile_size, tw, tile_size, 3)
    # uint8 相减不需要转 int16：maximum - minimum 恒为非负且不溢出，还省一次类型转换
    d = np.maximum(a, b) - np.minimum(a, b)
    per_tile = d.max(axis=(1, 3, 4))          # (th, tw)，通道一起归约（关键优化）

    ys, xs = np.nonzero(per_tile > threshold)
    coords = [(int(y), int(x)) for y, x in zip(ys, xs)]
    ratio = len(coords) / float(th * tw) if th * tw else 0.0
    return coords, ratio, th, tw


def build_atlas(img: Image.Image, coords: Sequence[Tuple[int, int]], tile_size: int):
    """把变化块拼成一张 atlas 图。

    返回 ``(atlas 图, tiles)``，其中 tiles 每项 ``(ty, tx, ax, ay)``
    —— 源画面第 (ty,tx) 块贴到 atlas 的 (ax,ay) 块位置。
    """
    n = len(coords)
    if n == 0:
        return None, []
    cols = int(np.ceil(np.sqrt(n)))
    atlas = Image.new("RGB", (cols * tile_size, ((n + cols - 1) // cols) * tile_size))
    tiles: List[Tile] = []
    for i, (ty, tx) in enumerate(coords):
        ax, ay = i % cols, i // cols
        box = (tx * tile_size, ty * tile_size, (tx + 1) * tile_size, (ty + 1) * tile_size)
        atlas.paste(img.crop(box), (ax * tile_size, ay * tile_size))
        tiles.append((ty, tx, ax, ay))
    return atlas, tiles


def encode_jpeg(img: Image.Image, quality: int, subsampling: int = 2) -> bytes:
    """JPEG 编码。

    subsampling=2 (4:2:0) 体积小；subsampling=0 (4:4:4) 文字更锐利但大 40% 左右。
    实测 q60 是甜点：q45 体积只小一点但画质明显下降，q75/q80 体积涨得快。
    """
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(1, min(100, int(quality))),
             subsampling=subsampling, optimize=False)
    return buf.getvalue()


def target_size(src_w: int, src_h: int, target_width: int, tile_size: int):
    """算出缩放后的尺寸（对齐到分块整数倍）。

    返回 ``(宽, 高, 缩放倍数)``。缩放倍数 > 1 表示要整数倍降采样，
    = 1 表示用原生分辨率。
    """
    if not target_width or target_width >= src_w:
        return (src_w // tile_size) * tile_size, (src_h // tile_size) * tile_size, 1

    factor = max(1, int(round(src_w / float(target_width))))
    if factor == 1:
        return (src_w // tile_size) * tile_size, (src_h // tile_size) * tile_size, 1
    # reduce() 要求两个维度都能被整除，否则退回 resize
    if src_w % factor or src_h % factor:
        factor = 1
    w = (src_w // factor) // tile_size * tile_size
    h = (src_h // factor) // tile_size * tile_size
    return max(w, tile_size), max(h, tile_size), factor


def downscale(img: Image.Image, factor: int) -> Tuple[Image.Image, str]:
    """整数倍降采样。

    ``reduce()`` 是快速盒式滤波，质量好；这是唯一被允许的降采样方式。
    """
    if factor <= 1:
        return img, "原生"
    return img.reduce(factor), "reduce(%d)" % factor


def encode_full_frame(img: Image.Image, quality: int) -> bytes:
    """整帧编码（关键帧用）。"""
    return encode_jpeg(img, quality)


class AdaptiveQuality:
    """按发送压力自适应调整 JPEG 质量。

    思路很直接：发送队列积压就降质量，队列长期空闲就慢慢升回去。
    比按 RTT 猜要简单可靠得多 —— 队列深度本身就是最直接的拥塞信号。
    """

    def __init__(self, q_min: int = 40, q_max: int = 80, start: int = 60,
                 queue_max: int = 8):
        self.q_min = q_min
        self.q_max = q_max
        self.quality = max(q_min, min(q_max, start))
        self.queue_max = max(1, queue_max)
        self._good_streak = 0

    def observe(self, queue_depth: int) -> int:
        """按当前队列深度更新质量，返回本次应使用的质量。"""
        if queue_depth >= self.queue_max:
            # 积压严重：明显降质
            self.quality = max(self.q_min, self.quality - 10)
            self._good_streak = 0
        elif queue_depth >= max(2, self.queue_max // 2):
            self.quality = max(self.q_min, self.quality - 4)
            self._good_streak = 0
        elif queue_depth <= 1:
            # 队列空闲：连续多次才慢慢升回去，避免来回抖动
            self._good_streak += 1
            if self._good_streak >= 10:
                self.quality = min(self.q_max, self.quality + 2)
                self._good_streak = 0
        return self.quality
