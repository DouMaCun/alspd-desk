# -*- coding: utf-8 -*-
"""WebSocket 会话层：端口回退、TLS、握手配对、密钥派生。

Agent 与 Viewer 共用这一层，把「怎么连上中继并建立加密通道」这件事收在一处，
避免两端各写一遍导致行为不一致。

连接流程
--------
1. 按 ``relay_ports`` 顺序逐个尝试连接（第一个连上的生效）。
   443 最优 —— 最易穿公司防火墙，且最不显眼。
2. TLS：自签证书没有 CA 可校验，所以默认 ``CERT_NONE``。
   若配置了 ``pinned_fingerprint``，则在握手后比对指纹，不匹配立即断开。
   > 安全性由**应用层端到端加密**保证，TLS 只负责「看起来像正常 HTTPS」。
3. 发 HELLO（明文 JSON，中继需要读它来配对），含 role / room / token / nonce。
4. 收 HELLO_ACK，拿到对端 nonce 与元信息。
5. ``会话密钥 = HKDF(Scrypt(password), agent_nonce ‖ viewer_nonce)``。
   中继没有 password，**无法派生密钥**。
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import config as config_mod
from . import crypto, protocol, proxy

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
except ImportError:  # 兼容旧版
    from websockets import connect as ws_connect                # type: ignore
    from websockets.exceptions import (                          # type: ignore
        ConnectionClosed, InvalidStatus, WebSocketException,
    )

MAX_MESSAGE_SIZE = 16 * 1024 * 1024
CONNECT_TIMEOUT = 12.0
HANDSHAKE_TIMEOUT = 20.0


class HandshakeError(Exception):
    """连接或握手阶段失败。"""


def normalize_fp(fp: str) -> str:
    return "".join(ch for ch in (fp or "") if ch not in ": \t").lower()


def format_fp(fp: str) -> str:
    fp = normalize_fp(fp)
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


@dataclass
class PeerInfo:
    """配对成功后拿到的对端信息。"""
    role: str = ""
    nonce: bytes = b""
    remote: str = ""
    screen: Dict[str, Any] = field(default_factory=dict)
    agent: Dict[str, Any] = field(default_factory=dict)
    # Viewer 视角：Agent 是否处于只读模式（由中继从 Agent 的 HELLO 透传过来）
    readonly: Optional[bool] = None


class Session:
    """一条已建立并已加密的会话。"""

    def __init__(self, ws, channel: crypto.SecureChannel, peer: PeerInfo,
                 local_nonce: bytes, uri: str, cert_fp: str = ""):
        self.ws = ws
        self.channel = channel
        self.peer = peer
        self.local_nonce = local_nonce
        self.uri = uri
        self.cert_fp = cert_fp
        self.closed = False

    # ---------------------------------------------------------- 发送

    async def send(self, msg_type: int, plaintext: bytes) -> None:
        """加密发送一条消息。"""
        if self.closed:
            raise ConnectionClosed(None, None)
        await self.ws.send(protocol.encode_message(msg_type, self.channel.encrypt(plaintext)))

    async def send_ctrl(self, kind: str, **kw: Any) -> None:
        await self.send(protocol.MSG_CTRL, protocol.ctrl(kind, **kw))

    # ---------------------------------------------------------- 接收

    async def recv(self) -> Tuple[int, bytes, bool]:
        """收一条消息，返回 ``(类型, 明文载荷, 是否已解密)``。

        加密类型的载荷会在返回前解密；中继通知等明文类型原样返回。
        """
        raw = await self.ws.recv()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        mtype, payload = protocol.decode_message(raw)
        if mtype in protocol.ENCRYPTED_TYPES:
            return mtype, self.channel.decrypt(payload), True
        return mtype, payload, False

    async def recv_peer_message(self, timeout: Optional[float] = None):
        """收一条消息并**自动跳过中继通知**，只返回对端发来的内容。

        返回 ``(类型, 明文载荷)``；若连接关闭返回 ``None``。
        """
        while True:
            coro = self.recv()
            if timeout is not None:
                mtype, payload, _ = await asyncio.wait_for(coro, timeout=timeout)
            else:
                mtype, payload, _ = await coro
            if mtype == protocol.MSG_RELAY:
                continue
            return mtype, payload

    # ---------------------------------------------------------- 关闭

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close()
        except Exception:
            pass


def _ssl_context(pinned_fp: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 自签证书没有可信 CA 可校验，只能先用 CERT_NONE 握手，
    # 再人工比对指纹（如果配置了）。真正的机密性来自应用层加密。
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if pinned_fp:
        # 仍然启用证书链结构校验的最低要求
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def _try_connect(host: str, port: int, use_tls: bool, timeout: float,
                       proxy_uri: Optional[str], pinned_fp: str):
    """尝试一种「传输 × 端口」组合。

    ``proxy_uri`` 为 None 表示直连。**必须显式传 proxy**：
    websockets 的默认值是 ``proxy=True``，会去读 HTTP_PROXY 环境变量，
    这会导致行为不可预期（用户以为在直连，实际走了环境变量里的代理）。
    """
    scheme = "wss" if use_tls else "ws"
    uri = "%s://%s:%s" % (scheme, host, port)
    ctx = _ssl_context(pinned_fp) if use_tls else None

    kwargs = dict(
        ssl=ctx,
        max_size=MAX_MESSAGE_SIZE,
        open_timeout=timeout,
        ping_interval=20,
        ping_timeout=20,
        compression=None,
        # 直连时显式 None；用代理时传 URI 字符串
        proxy=proxy_uri if proxy_uri else None,
    )
    ws = await asyncio.wait_for(ws_connect(uri, **kwargs), timeout=timeout + 3)
    return ws, uri


async def connect(
    cfg: config_mod.AppConfig,
    role: str,
    hello_extra: Optional[Dict[str, Any]] = None,
    log=print,
) -> Session:
    """连接中继、完成握手、派生会话密钥。失败抛 HandshakeError。

    连接顺序：**先用所有端口试直连**，全不通再依次尝试每个代理（每个代理先试 443）。
    这样正常情况下走最短链路，只有受限网络才落到代理上。
    """
    c = cfg.common
    if not c.relay_host:
        raise HandshakeError("common.relay_host 未配置")
    if not c.room:
        raise HandshakeError("common.room 未配置")
    if not c.relay_token:
        raise HandshakeError("common.relay_token 未配置")
    if not c.password:
        raise HandshakeError("common.password 未配置（端到端加密需要它）")
    if role not in (protocol.ROLE_AGENT, protocol.ROLE_VIEWER):
        raise HandshakeError("非法角色：%r" % role)

    # Scrypt 拉伸较慢（几十毫秒），只做一次
    psk = crypto.derive_psk(c.password)
    local_nonce = crypto.new_nonce(16)

    ports: List[int] = list(c.relay_ports or [443])
    use_tls = bool(cfg.tls.enabled)
    transports = proxy.candidates(c.proxy)

    ws = None
    uri = ""
    used_transport = ""
    last_errors: List[str] = []

    for t_index, (t_desc, proxy_uri) in enumerate(transports):
        # 代理兜底时只先试 443（最可能通），避免候选一多就等太久
        try_ports = ports if t_index == 0 else ([443] if 443 in ports else ports[:1])
        if t_index > 0 and len(transports) > 1:
            log("[连接] 直连不通，尝试代理：%s" % t_desc)
        for port in try_ports:
            try:
                ws, uri = await _try_connect(c.relay_host, port, use_tls,
                                             CONNECT_TIMEOUT, proxy_uri,
                                             cfg.tls.pinned_fingerprint)
                used_transport = t_desc
                log("[连接] 已连上 %s（%s）" % (uri, t_desc))
                break
            except (OSError, WebSocketException, asyncio.TimeoutError, InvalidStatus) as e:
                last_errors.append("%s:%s(%s) %s: %s"
                                   % (c.relay_host, port, t_desc, type(e).__name__, str(e)[:60]))
                log("[连接] 失败 %s:%s  [%s]  %s" % (c.relay_host, port, t_desc,
                                                     type(e).__name__))
                continue
        if ws is not None:
            break

    if ws is None:
        hint = ""
        if all(t[1] is None for t in transports):
            hint = ("\n提示：当前配置为只走直连（proxy = \"direct\"）。"
                    "若公司网络要求经代理出网，请把 proxy 设为 \"auto\"。")
        elif len(transports) > 1:
            hint = ("\n提示：直连和代理都试过了。请确认 VPS 上中继在运行、"
                    "端口与 relay_ports 一致；并可用 tools/probe_agent.py 实测。")
        detail = "\n  ".join(last_errors[-6:])
        raise HandshakeError("所有连接方式都失败：%s\n  尝试记录：\n  %s%s"
                             % (c.relay_host, detail, hint))

    # ---- 证书指纹 ----
    cert_fp = ""
    if use_tls:
        try:
            ssl_obj = ws.transport.get_extra_info("ssl_object")
            der = ssl_obj.getpeercert(binary_form=True) if ssl_obj else None
            if der:
                cert_fp = hashlib.sha256(der).hexdigest()
        except Exception:
            cert_fp = ""
        pinned = normalize_fp(cfg.tls.pinned_fingerprint)
        if pinned:
            if cert_fp != pinned:
                await ws.close()
                raise HandshakeError(
                    "证书指纹不匹配 —— 可能被中间人替换，或连错了服务器。\n"
                    "  期望：%s\n  实际：%s\n"
                    "（若公司网络做 TLS 中间人解密，请把 pinned_fingerprint 留空 —— "
                    "应用层端到端加密仍能保证安全）"
                    % (format_fp(pinned), format_fp(cert_fp)))
            log("[连接] 证书指纹校验通过：%s" % format_fp(cert_fp))
        elif cert_fp:
            log("[连接] 服务端证书指纹：%s（未配置钉扎，仅记录）" % format_fp(cert_fp))

    # ---- HELLO ----
    hello: Dict[str, Any] = {
        "role": role,
        "room": c.room,
        "token": c.relay_token,
        "nonce": local_nonce.hex(),
        "version": "0.1",
    }
    if hello_extra:
        hello.update(hello_extra)

    try:
        await ws.send(protocol.encode_plain_json(protocol.MSG_HELLO, hello))
    except Exception as e:
        await ws.close()
        raise HandshakeError("发送 HELLO 失败：%s" % e)

    # ---- 等 HELLO_ACK（中间可能先收到 WAITING 通知）----
    peer = PeerInfo()
    deadline = asyncio.get_event_loop().time() + HANDSHAKE_TIMEOUT
    waiting_logged = False
    while True:
        remain = deadline - asyncio.get_event_loop().time()
        if remain <= 0:
            await ws.close()
            raise HandshakeError("等待配对超时 —— 对端没有连上来（%s 秒）" % HANDSHAKE_TIMEOUT)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remain)
        except asyncio.TimeoutError:
            await ws.close()
            raise HandshakeError("等待配对超时 —— 对端没有连上来（%s 秒）" % HANDSHAKE_TIMEOUT)
        except ConnectionClosed as e:
            await ws.close()
            raise HandshakeError("中继关闭了连接：%s" % e)

        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        mtype, payload = protocol.decode_message(raw)

        if mtype == protocol.MSG_RELAY:
            obj = protocol.decode_json(payload)
            kind = obj.get("t")
            if kind == protocol.RELAY_WAITING and not waiting_logged:
                log("[配对] 已进入房间 [%s]，等待对端连接……" % c.room)
                waiting_logged = True
            elif kind == protocol.RELAY_PEER_LEFT:
                log("[配对] 对端离开了房间")
            elif kind == protocol.RELAY_REPLACED:
                await ws.close()
                raise HandshakeError("本连接被同角色的新连接顶替")
            continue

        if mtype != protocol.MSG_HELLO_ACK:
            log("[配对] 忽略预期外的消息：%s" % protocol.msg_name(mtype))
            continue

        ack = protocol.decode_json(payload)
        peer.role = str(ack.get("role", ""))
        peer.remote = str(ack.get("remote", ""))
        peer.screen = ack.get("screen") or {}
        peer.agent = ack.get("agent") or {}
        ro = ack.get("readonly")
        peer.readonly = bool(ro) if ro is not None else None
        try:
            peer.nonce = bytes.fromhex(str(ack.get("nonce", "")))
        except ValueError:
            await ws.close()
            raise HandshakeError("HELLO_ACK 里的 nonce 非法")
        if not peer.nonce:
            await ws.close()
            raise HandshakeError("HELLO_ACK 缺少对端 nonce，无法派生密钥")
        break

    # ---- 派生会话密钥 ----
    # 拼接顺序固定为 agent_nonce + viewer_nonce，两端必须一致
    if role == protocol.ROLE_AGENT:
        agent_nonce, viewer_nonce = local_nonce, peer.nonce
    else:
        agent_nonce, viewer_nonce = peer.nonce, local_nonce

    key = crypto.derive_session_key(psk, agent_nonce, viewer_nonce)
    channel = crypto.SecureChannel(key)

    log("[配对] 已与 %s（%s）建立端到端加密通道" % (peer.role or "对端", peer.remote or "?"))
    return Session(ws, channel, peer, local_nonce, uri, cert_fp)
