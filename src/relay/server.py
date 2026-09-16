#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relay/server.py —— 中继服务器（部署在 VPS）

职责
----
把公司电脑（Agent）和家里电脑（Viewer）两条**主动外连**的 WebSocket 连接按房间配对，
然后**原样转发**它们之间的消息。

安全边界（重要）
----------------
* 中继只读得到 HELLO 握手（JSON 明文），因为需要靠它来配对。
* 握手之后的 FRAME / INPUT 载荷都是**端到端 AES-256-GCM 密文**，
  中继没有 password，**无法解密**。
* 中继持有的 relay_token 只能验证「谁可以进这个房间」，不能解密内容。
* 日志只记录消息类型与字节数，**从不记录载荷内容**。

因为 Agent 与 Viewer 都是主动外连，被控机不需要开放任何入站端口、
也不需要改防火墙。

用法
----
    # 用配置文件
    sudo python3 src/relay/server.py --config config.toml

    # 命令行直填（TLS 证书不存在会自动生成自签证书）
    sudo python3 src/relay/server.py --port 443 --token <32位随机串> --room home --tls

    # 只校验配置，不启动
    python3 src/relay/server.py --config config.toml --check
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import logging
import os
import pathlib
import ssl
import subprocess
import sys
import time
from typing import Any, Dict, Optional

# 让 `python src/relay/server.py` 能直接 import common
_SRC = pathlib.Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import config as config_mod          # noqa: E402
from common import protocol                      # noqa: E402
from common.console import setup_console         # noqa: E402

setup_console()

try:
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
except ImportError:  # 兼容旧版 websockets
    print("需要 websockets 库：pip install 'websockets>=13'")
    raise

LOG = logging.getLogger("relay")

HANDSHAKE_TIMEOUT = 15.0        # 握手超时：连上后这么久没完成 HELLO 就断开
MAX_MESSAGE_SIZE = 16 * 1024 * 1024
ROLES = (protocol.ROLE_AGENT, protocol.ROLE_VIEWER)


def peer_role(role: str) -> str:
    return protocol.ROLE_VIEWER if role == protocol.ROLE_AGENT else protocol.ROLE_AGENT


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    if n < 1024 ** 3:
        return "%.2f MB" % (n / 1024.0 / 1024.0)
    return "%.2f GB" % (n / 1024.0 ** 3)


def fmt_fp(fp: str) -> str:
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


class Room:
    """一个房间：分别持有 agent 与 viewer 两条连接。"""

    def __init__(self, name: str):
        self.name = name
        self.peers: Dict[str, "Peer"] = {}
        self.created = time.time()

    def other(self, role: str) -> Optional["Peer"]:
        return self.peers.get(peer_role(role))

    def is_full(self) -> bool:
        return all(r in self.peers for r in ROLES)

    def describe(self) -> str:
        return ", ".join("%s=%s" % (r, self.peers[r].remote) for r in ROLES if r in self.peers)


class Peer:
    def __init__(self, ws, role: str, room: Room, nonce: bytes, hello: Dict[str, Any], remote: str):
        self.ws = ws
        self.role = role
        self.room = room
        self.nonce = nonce
        self.hello = hello
        self.remote = remote
        self.started = time.time()
        self.bytes_in = 0
        self.msgs_in = 0
        self.bytes_out = 0
        self.msgs_out = 0

    def __repr__(self) -> str:
        return "<%s %s>" % (self.role, self.remote)


rooms: Dict[str, Room] = {}
rooms_lock = asyncio.Lock()


async def send_message(peer: Peer, msg_type: int, obj: Dict[str, Any]) -> None:
    """发送明文 JSON 消息（只用于 HELLO_ACK 与 RELAY 通知）。"""
    await peer.ws.send(protocol.encode_plain_json(msg_type, obj))
    peer.msgs_out += 1


async def notify_pairing(room: Room) -> None:
    """两边都到齐了，把各自的 nonce 与元信息交换给对方，用于派生会话密钥。"""
    agent = room.peers.get(protocol.ROLE_AGENT)
    viewer = room.peers.get(protocol.ROLE_VIEWER)
    if agent is None or viewer is None:
        return

    # Agent 需要 Viewer 的 nonce；Viewer 需要 Agent 的 nonce
    await send_message(agent, protocol.MSG_HELLO_ACK, {
        "role": viewer.role,
        "nonce": viewer.nonce.hex(),
        "remote": viewer.remote,
    })
    await send_message(viewer, protocol.MSG_HELLO_ACK, {
        "role": agent.role,
        "nonce": agent.nonce.hex(),
        "remote": agent.remote,
        "screen": agent.hello.get("screen") or {},
        "agent": agent.hello.get("agent") or {},
        # 把 Agent 的只读状态透传给 Viewer —— 让 Viewer 能在界面上明确告警
        "readonly": agent.hello.get("readonly"),
    })
    LOG.info("房间 [%s] 已配对：%s  <->  %s", room.name, agent.remote, viewer.remote)


async def unpair_notify(room: Room, leaving: str) -> None:
    """一端离开时通知另一端。"""
    other = room.other(leaving)
    if other is not None:
        try:
            await send_message(other, protocol.MSG_RELAY,
                               {"t": protocol.RELAY_PEER_LEFT, "role": leaving})
        except ConnectionClosed:
            pass


async def handle(ws) -> None:
    remote = "?"
    try:
        try:
            addrs = ws.remote_address
            if addrs:
                remote = "%s:%s" % (addrs[0], addrs[1])
        except Exception:
            pass

        # ---------- 握手 ----------
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            LOG.warning("%s 连接后 %.0fs 未握手，断开", remote, HANDSHAKE_TIMEOUT)
            await ws.close(code=1008, reason="handshake timeout")
            return

        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        try:
            msg_type, payload = protocol.decode_message(raw)
        except protocol.ProtocolError as e:
            LOG.warning("%s 首包非法：%s", remote, e)
            await ws.close(code=1008, reason="bad handshake")
            return

        if msg_type != protocol.MSG_HELLO:
            LOG.warning("%s 首包不是 HELLO（类型 0x%02x），断开", remote, msg_type)
            await ws.close(code=1008, reason="expected HELLO")
            return

        try:
            hello = protocol.decode_json(payload)
        except protocol.ProtocolError as e:
            LOG.warning("%s HELLO 解析失败：%s", remote, e)
            await ws.close(code=1008, reason="bad hello")
            return

        role = str(hello.get("role", ""))
        room_name = str(hello.get("room", ""))
        token = str(hello.get("token", ""))
        nonce_hex = str(hello.get("nonce", ""))

        if role not in ROLES:
            LOG.warning("%s HELLO 的 role 非法：%r", remote, role)
            await ws.close(code=1008, reason="bad role")
            return
        if not room_name:
            LOG.warning("%s HELLO 缺少 room", remote)
            await ws.close(code=1008, reason="no room")
            return
        # 房间限制（若配置了）。注意：房间名不是安全边界 ——
        # 真正的保护是端到端加密，这里只是防止误连到别人的房间。
        if expected_room and room_name != expected_room:
            LOG.warning("%s 房间号不匹配：收到 [%s]，本机只服务 [%s]，拒绝（role=%s）",
                        remote, room_name, expected_room, role)
            await ws.close(code=1008, reason="wrong room")
            return
        # 令牌用常量时间比较，避免时序侧信道
        if not hmac.compare_digest(token, expected_token):
            LOG.warning("%s 房间 [%s] 令牌校验失败，拒绝（role=%s）", remote, room_name, role)
            await ws.close(code=1008, reason="bad token")
            return
        try:
            nonce = bytes.fromhex(nonce_hex)
            if len(nonce) < 8:
                raise ValueError("nonce 太短")
        except ValueError as e:
            LOG.warning("%s HELLO 的 nonce 非法：%s", remote, e)
            await ws.close(code=1008, reason="bad nonce")
            return

        # ---------- 入房 ----------
        async with rooms_lock:
            room = rooms.get(room_name)
            if room is None:
                room = Room(room_name)
                rooms[room_name] = room
                LOG.info("房间 [%s] 已创建", room_name)

            old = room.peers.get(role)
            if old is not None:
                LOG.info("房间 [%s] 的 %s 角色被新连接顶替（旧 %s）", room_name, role, old.remote)
                try:
                    await old.ws.close(code=1000, reason="replaced by new connection")
                except Exception:
                    pass

            peer = Peer(ws, role, room, nonce, hello, remote)
            room.peers[role] = peer

        screen = hello.get("screen") or {}
        LOG.info("房间 [%s] +%s 来自 %s  屏幕=%sx%s  只读模式=%s",
                 room_name, role, remote,
                 screen.get("width", "?"), screen.get("height", "?"),
                 hello.get("readonly", "?"))

        if room.is_full():
            await notify_pairing(room)
        else:
            LOG.info("房间 [%s] 等待对端（当前：%s）", room_name, room.describe() or "空")
            await send_message(peer, protocol.MSG_RELAY,
                               {"t": protocol.RELAY_WAITING, "room": room_name})

        # ---------- 转发 ----------
        async for message in ws:
            if isinstance(message, str):
                message = message.encode("utf-8")
            peer.msgs_in += 1
            peer.bytes_in += len(message)

            target = room.other(role)
            if target is None:
                # 对端不在，直接丢弃。不缓存 —— 远程桌面缓存只会造成延迟。
                continue
            try:
                await target.ws.send(message)
            except ConnectionClosed:
                LOG.info("房间 [%s] 转发失败：%s 已断开", room_name, target.role)
                break
            target.msgs_out += 1
            target.bytes_out += len(message)

            # 只记录类型与大小，绝不记录载荷内容
            if LOG.isEnabledFor(logging.DEBUG):
                try:
                    LOG.debug("房间 [%s] %s -> %s  %s  %s",
                              room_name, role, target.role,
                              protocol.msg_name(message[0]),
                              fmt_bytes(len(message)))
                except Exception:
                    pass

    except ConnectionClosed:
        pass
    except Exception:
        LOG.exception("处理连接时出现未预期异常（%s）", remote)
    finally:
        await cleanup(ws, remote)


async def cleanup(ws, remote: str) -> None:
    async with rooms_lock:
        gone: Optional[Peer] = None
        for name, room in list(rooms.items()):
            for role, peer in list(room.peers.items()):
                if peer.ws is ws:
                    gone = room.peers.pop(role)
                    LOG.info("房间 [%s] -%s 来自 %s  会话 %.1fs  收 %s/%d 条  发 %s/%d 条",
                             name, role, remote, time.time() - gone.started,
                             fmt_bytes(gone.bytes_in), gone.msgs_in,
                             fmt_bytes(gone.bytes_out), gone.msgs_out)
                    if not room.peers:
                        del rooms[name]
                        LOG.info("房间 [%s] 已空，回收", name)
                    break
            if gone is not None:
                break

    if gone is not None:
        # 通知仍在房间里的对端
        room = gone.room
        if room.name in rooms:
            await unpair_notify(room, gone.role)


# ---------------------------------------------------------------- 证书

def ensure_self_signed(cert: str, key: str) -> Optional[str]:
    """证书不存在就用 openssl 现场生成，返回 SHA256 指纹。"""
    if os.path.exists(cert) and os.path.exists(key):
        try:
            with open(cert, "r", encoding="utf-8") as f:
                pem = f.read()
            return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        except Exception:
            return None

    os.makedirs(os.path.dirname(os.path.abspath(cert)) or ".", exist_ok=True)
    LOG.info("生成自签证书 -> %s", cert)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", cert, "-days", "3650",
             "-subj", "/CN=alspd-desk-relay"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        LOG.error("找不到 openssl，请先安装：apt install -y openssl")
        return None
    except subprocess.CalledProcessError as e:
        LOG.error("openssl 生成证书失败：%s", e)
        return None

    with open(cert, "r", encoding="utf-8") as f:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()


def build_ssl_context(cert: str, key: str):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


# ---------------------------------------------------------------- main

expected_token = ""
expected_room = ""


def main() -> int:
    global expected_token, expected_room

    ap = argparse.ArgumentParser(description="ALSPD-DESK 中继服务器")
    ap.add_argument("--config", default=None, help="配置文件路径（默认自动查找 config.toml）")
    ap.add_argument("--host", default=None, help="监听地址，默认取自配置或 0.0.0.0")
    ap.add_argument("--port", type=int, default=None, help="监听端口")
    ap.add_argument("--token", default=None, help="配对令牌（覆盖配置）")
    ap.add_argument("--room", default=None, help="房间号（覆盖配置；留空表示接受任意房间）")
    ap.add_argument("--tls", action="store_true", help="启用 TLS")
    ap.add_argument("--no-tls", action="store_true", help="禁用 TLS（仅本机调试用）")
    ap.add_argument("--cert", default=None, help="证书路径")
    ap.add_argument("--key", default=None, help="私钥路径")
    ap.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    ap.add_argument("--check", action="store_true", help="只校验配置后退出")
    args = ap.parse_args()

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print("配置错误：%s" % e)
        return 2

    level = (args.log_level or cfg.logging.level or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    host = args.host or cfg.relay.host or "0.0.0.0"
    port = args.port if args.port is not None else (cfg.relay.port or 443)
    expected_token = args.token or cfg.common.relay_token
    # 房间限制：留空则接受任意房间名（仍然要求正确令牌）
    expected_room = args.room if args.room is not None else cfg.common.room

    print()
    print("=" * 70)
    print("  ALSPD-DESK  中继服务器")
    print("=" * 70)
    print("  配置文件    : %s" % (cfg.path or "(未找到，使用默认值)"))
    print("  监听        : %s:%s" % (host, port))
    print("  房间限制    : %s" % (expected_room or "(不限制，接受任意房间名)"))
    if cfg.logging.file:
        print("  日志文件    : %s" % cfg.logging.file)

    problems = []
    if not expected_token:
        problems.append("未设置配对令牌（--token 或 common.relay_token）")
    elif len(expected_token) < 16:
        problems.append("配对令牌太短（少于 16 位），建议 32 位以上")
    if problems:
        for p in problems:
            print("  ❌ %s" % p)
        print("=" * 70)
        return 2

    if cfg.common.password:
        print("  ⚠️  检测到配置里填了 common.password —— 中继**不需要**它。")
        print("      端到端加密密码只应给 Agent 与 Viewer。中继拿到它并不会更安全，")
        print("      反而违背了「中继看不到内容」的设计。建议从 VPS 的配置里删掉。")

    use_tls = cfg.tls.enabled
    if args.tls:
        use_tls = True
    if args.no_tls:
        use_tls = False

    ssl_ctx = None
    if use_tls:
        cert = args.cert or cfg.relay.cert
        key = args.key or cfg.relay.key
        if not cert or not key:
            cert = cert or "relay.crt"
            key = key or "relay.key"
            print("  证书        : 未配置，将在当前目录生成自签证书 %s" % cert)
        fp = ensure_self_signed(cert, key)
        if fp is None:
            print("  ❌ TLS 证书准备失败")
            print("=" * 70)
            return 2
        try:
            ssl_ctx = build_ssl_context(cert, key)
        except (OSError, ssl.SSLError) as e:
            print("  ❌ 加载证书失败：%s" % e)
            print("=" * 70)
            return 2
        print("  证书        : %s" % cert)
        print("  证书 SHA256 : %s" % fmt_fp(fp))
        print("  -> 如需钉扎校验，把上面指纹填到两端的 tls.pinned_fingerprint")
        print("     （但若公司网络做 TLS 中间人解密，请留空，否则连不上）")
    else:
        print("  TLS         : 已禁用（仅建议本机调试用）")

    print("=" * 70)
    print()

    if args.check:
        print("  配置校验通过（--check 模式，未启动服务）")
        return 0

    if cfg.logging.file:
        fh = logging.FileHandler(cfg.logging.file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s"))
        logging.getLogger().addHandler(fh)

    try:
        asyncio.run(serve_forever(host, port, ssl_ctx))
    except KeyboardInterrupt:
        print("\n  已停止")
    return 0


async def serve_forever(host: str, port: int, ssl_ctx) -> None:
    # ping_interval 让死连接能被及时发现；max_size 给足大帧空间
    async with serve(
        handle, host, port, ssl=ssl_ctx,
        max_size=MAX_MESSAGE_SIZE,
        ping_interval=20, ping_timeout=20,
        compression=None,          # 已是 JPEG+密文，再压缩纯属浪费 CPU
    ):
        LOG.info("中继已启动，监听 %s:%s（%s）", host, port, "wss/TLS" if ssl_ctx else "ws/明文")
        LOG.info("等待 Agent 与 Viewer 连接…… 按 Ctrl-C 停止")
        await asyncio.Future()


if __name__ == "__main__":
    sys.exit(main())
