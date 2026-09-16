# -*- coding: utf-8 -*-
"""消息协议：类型定义、消息编解码、视频帧包编解码。

线上格式
--------
WebSocket 的每条消息 = ``[1 字节类型][载荷]``。

类型分成两类，**这是刻意的设计**，避免「载荷到底是明文还是密文」产生歧义：

===================  ==========================================================
明文类型             载荷是 UTF-8 JSON。中继必须读得懂，因为要靠它配对。
(HELLO/HELLO_ACK/
 RELAY)
-------------------  ----------------------------------------------------------
加密类型             载荷是 ``SecureChannel`` 产出的端到端密文。
(CTRL/FRAME/INPUT)   解密后的明文才对应对面的结构（JSON 或帧包）。
                     中继读不懂，只能原样转发。
===================  ==========================================================

配套的载荷约定（**没有例外**，早期版本在这里踩过坑）：

* 构造函数一律只产出**载荷**，不含类型字节：
  ``ctrl()`` / ``input_mouse()`` / ``input_key()`` … 返回的都是载荷。
* 调用方用 ``encode_message(类型, 载荷)`` 自己加类型字节。
* 需要「明文 JSON 消息」时用 ``encode_plain_json(类型, obj)``。

视频帧包（FRAME 解密后的明文布局）
----------------------------------
::

    offset  size          字段
    0       1             flags        （bit0 = 关键帧）
    1       2             atlas_w      uint16 大端
    3       2             atlas_h      uint16 大端
    5       2             tile_count   uint16 大端
    7       1             quality      JPEG 质量
    8       tile_count*8  tiles        每块 ty,tx,ax,ay 各 uint16 大端
    ...     剩余           JPEG 数据

* **普通帧**：tiles 描述「源画面第 (ty,tx) 块贴到 atlas 的 (ax,ay)」，
  JPEG 是 atlas 拼接图。实测 atlas 比逐块编码省 44% 体积、只需一次编码调用。
* **关键帧**（flags bit0 = 1）：tile_count = 0，JPEG 即整屏图像，Viewer 整屏替换，
  用于修正累积误差。

坐标一律以「块」为单位（不是像素），乘 tile_size 即得像素位置。
"""

from __future__ import annotations

import json
import struct
from typing import Any, Dict, List, Sequence, Set, Tuple

# ---------------------------------------------------------------- 消息类型

# ---- 明文类型：中继需要读取 ----
MSG_HELLO = 0x01        # 客户端 -> 中继：握手（room / token / nonce / 屏幕尺寸）
MSG_HELLO_ACK = 0x02    # 中继 -> 客户端：配对成功，附带对端 nonce
MSG_RELAY = 0x07        # 中继 -> 客户端：通知（waiting / peer_left / replaced）

# ---- 加密类型：载荷是端到端密文，中继读不懂 ----
MSG_CTRL = 0x03         # 控制消息，解密后是 JSON
MSG_FRAME = 0x04        # Agent -> Viewer：视频帧包
MSG_INPUT = 0x05        # Viewer -> Agent：键鼠事件，解密后是 JSON
MSG_BYE = 0x06          # 任一端 -> 中继：主动断开

PLAINTEXT_TYPES: Set[int] = {MSG_HELLO, MSG_HELLO_ACK, MSG_RELAY}
ENCRYPTED_TYPES: Set[int] = {MSG_CTRL, MSG_FRAME, MSG_INPUT}

MSG_NAMES = {
    MSG_HELLO: "HELLO",
    MSG_HELLO_ACK: "HELLO_ACK",
    MSG_CTRL: "CTRL",
    MSG_FRAME: "FRAME",
    MSG_INPUT: "INPUT",
    MSG_BYE: "BYE",
    MSG_RELAY: "RELAY",
}


def msg_name(msg_type: int) -> str:
    return MSG_NAMES.get(msg_type, "0x%02x" % msg_type)


ROLE_AGENT = "agent"
ROLE_VIEWER = "viewer"

# ---------------------------------------------------------------- 帧包

FLAG_KEYFRAME = 0x01

_FRAME_HEADER = struct.Struct(">BHHHB")     # flags, atlas_w, atlas_h, tile_count, quality
_TILE_ENTRY = struct.Struct(">HHHH")        # ty, tx, ax, ay
FRAME_HEADER_LEN = _FRAME_HEADER.size       # 8

# 单个消息上限，防止损坏/恶意数据导致内存爆炸（约 16MB）
MAX_MESSAGE_PAYLOAD = 16 * 1024 * 1024


class ProtocolError(Exception):
    """协议层错误。"""


# ---------------------------------------------------------------- 通用消息

def encode_message(msg_type: int, payload: bytes = b"") -> bytes:
    """组装 ``[类型][载荷]``。载荷是明文还是密文由类型决定。"""
    if not 0 <= msg_type <= 0xFF:
        raise ValueError("消息类型超出范围：%r" % (msg_type,))
    return bytes([msg_type]) + payload


def decode_message(data: bytes) -> Tuple[int, bytes]:
    """拆出 ``(类型, 载荷)``。"""
    if not data:
        raise ProtocolError("空消息")
    if len(data) > MAX_MESSAGE_PAYLOAD:
        raise ProtocolError("消息过大：%d 字节" % len(data))
    return data[0], data[1:]


def to_json(obj: Any) -> bytes:
    """对象 -> JSON 载荷（**不含**类型字节）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_plain_json(msg_type: int, obj: Any) -> bytes:
    """明文 JSON 消息（HELLO / HELLO_ACK / RELAY 用）。"""
    return encode_message(msg_type, to_json(obj))


def decode_json(payload: bytes) -> Dict[str, Any]:
    """JSON 载荷 -> 对象。"""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError("JSON 解析失败：%s" % e) from None
    if not isinstance(obj, dict):
        raise ProtocolError("JSON 载荷必须是对象，实际是 %s" % type(obj).__name__)
    return obj


# ---------------------------------------------------------------- 视频帧包

def encode_frame_packet(
    tiles: Sequence[Tuple[int, int, int, int]],
    atlas_w: int,
    atlas_h: int,
    quality: int,
    jpeg: bytes,
    keyframe: bool = False,
) -> bytes:
    """打包一个视频帧更新（**载荷**，不含消息类型字节）。

    tiles: 每项 ``(ty, tx, ax, ay)`` —— 源画面第 (ty,tx) 块贴到 atlas 的 (ax,ay)。
           关键帧时传空列表。
    """
    if keyframe:
        tiles = []
    flags = FLAG_KEYFRAME if keyframe else 0
    if not 0 <= atlas_w <= 0xFFFF or not 0 <= atlas_h <= 0xFFFF:
        raise ProtocolError("atlas 尺寸超出 uint16：%dx%d" % (atlas_w, atlas_h))
    if len(tiles) > 0xFFFF:
        raise ProtocolError("分块数量超出 uint16：%d" % len(tiles))

    out = bytearray()
    out += _FRAME_HEADER.pack(flags, atlas_w, atlas_h, len(tiles), max(0, min(100, int(quality))))
    for ty, tx, ax, ay in tiles:
        out += _TILE_ENTRY.pack(ty, tx, ax, ay)
    out += jpeg
    return bytes(out)


def decode_frame_packet(data: bytes) -> Dict[str, Any]:
    """解出帧包。返回 dict，``tiles`` 是 ``(ty, tx, ax, ay)`` 列表。"""
    if len(data) < FRAME_HEADER_LEN:
        raise ProtocolError("帧包过短：%d 字节" % len(data))
    if len(data) > MAX_MESSAGE_PAYLOAD:
        raise ProtocolError("帧包过大：%d 字节" % len(data))

    flags, atlas_w, atlas_h, tile_count, quality = _FRAME_HEADER.unpack_from(data, 0)
    off = FRAME_HEADER_LEN
    need = off + tile_count * _TILE_ENTRY.size
    if len(data) < need:
        raise ProtocolError("帧包截断：需要 %d 字节，实际 %d" % (need, len(data)))

    tiles: List[Tuple[int, int, int, int]] = []
    for _ in range(tile_count):
        tiles.append(_TILE_ENTRY.unpack_from(data, off))
        off += _TILE_ENTRY.size

    return {
        "keyframe": bool(flags & FLAG_KEYFRAME),
        "atlas_w": atlas_w,
        "atlas_h": atlas_h,
        "quality": quality,
        "tiles": tiles,
        "jpeg": data[off:],
    }


# ---------------------------------------------------------------- 控制消息（加密）

CTRL_KEYFRAME = "keyframe"      # Viewer -> Agent：请求整屏关键帧
CTRL_PARAMS = "params"          # Viewer -> Agent：调整参数（quality / max_fps / target_width）
CTRL_PING = "ping"              # 双向：RTT 测量
CTRL_PONG = "pong"
CTRL_STATS = "stats"            # Agent -> Viewer：状态上报（fps / 码率 / 变化块数）
CTRL_CLIPBOARD = "clipboard"    # 双向：剪贴板同步（纯文本）
CTRL_BYE = "bye"                # 主动断开

# 剪贴板单条内容上限，和 agent/clipboard.py 保持一致
MAX_CLIPBOARD_LEN = 1_000_000

# ---- 中继通知（明文 MSG_RELAY）----
RELAY_WAITING = "waiting"       # 已入房，等待对端
RELAY_PEER_LEFT = "peer_left"   # 对端离开
RELAY_REPLACED = "replaced"     # 本连接被同角色的新连接顶替


def ctrl(kind: str, **kw: Any) -> bytes:
    """构造控制消息**载荷**（调用方负责加密并加类型字节）。"""
    obj: Dict[str, Any] = {"t": kind}
    obj.update(kw)
    return to_json(obj)


def parse_ctrl(payload: bytes) -> Dict[str, Any]:
    obj = decode_json(payload)
    if "t" not in obj:
        raise ProtocolError("CTRL 消息缺少 t 字段")
    return obj


def relay_notice(kind: str, **kw: Any) -> bytes:
    """构造中继通知消息（明文，含类型字节）。"""
    obj: Dict[str, Any] = {"t": kind}
    obj.update(kw)
    return encode_plain_json(MSG_RELAY, obj)


def ctrl_clipboard(text: str) -> bytes:
    """剪贴板同步**载荷**。

    回环防护不在这里做 —— 两端各自在本地把「刚写入的内容」记为已见即可，
    见 agent/clipboard.py 的说明。
    """
    if len(text) > MAX_CLIPBOARD_LEN:
        raise ProtocolError("剪贴板内容过大：%d 字符（上限 %d）" % (len(text), MAX_CLIPBOARD_LEN))
    return ctrl(CTRL_CLIPBOARD, text=text)


def clipboard_text_from(msg: Dict[str, Any]) -> str:
    """从 CTRL 消息里取出剪贴板文本，非法返回空串。"""
    text = msg.get("text")
    if not isinstance(text, str) or not text:
        return ""
    if len(text) > MAX_CLIPBOARD_LEN:
        return ""
    return text


# ---------------------------------------------------------------- 键鼠事件（加密）

INPUT_MOUSE = "m"
INPUT_BUTTON = "b"
INPUT_WHEEL = "w"
INPUT_KEY = "k"


def input_mouse(x: float, y: float) -> bytes:
    """鼠标移动**载荷**。坐标是**归一化 0~1**，所以 Agent 端缩放与否都不影响正确性。"""
    return to_json({"t": INPUT_MOUSE, "x": round(float(x), 6), "y": round(float(y), 6)})


def input_button(button: int, down: bool, x: float, y: float) -> bytes:
    return to_json({
        "t": INPUT_BUTTON, "b": int(button), "d": 1 if down else 0,
        "x": round(float(x), 6), "y": round(float(y), 6),
    })


def input_wheel(delta: int, x: float, y: float) -> bytes:
    return to_json({
        "t": INPUT_WHEEL, "w": int(delta),
        "x": round(float(x), 6), "y": round(float(y), 6),
    })


def input_key(scancode: int, down: bool, extended: bool = False) -> bytes:
    """键盘事件用**扫描码**而非字符，这样尊重目标机器的键盘布局。"""
    return to_json({
        "t": INPUT_KEY, "sc": int(scancode), "d": 1 if down else 0,
        "ext": 1 if extended else 0,
    })


def parse_input(payload: bytes) -> Dict[str, Any]:
    obj = decode_json(payload)
    if "t" not in obj:
        raise ProtocolError("INPUT 消息缺少 t 字段")
    return obj
