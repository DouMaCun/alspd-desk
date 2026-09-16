# -*- coding: utf-8 -*-
"""端到端加密与密钥派生。

设计要点
--------
1. **密码不当密钥直接用**。先用 Scrypt 把用户密码拉伸成 32 字节 PSK，
   大幅提高离线暴力破解成本。
2. **会话密钥用 HKDF 派生**，salt 由双方的随机 nonce 组成。
   中继服务器没有 PSK，因此**无法派生会话密钥，看不到任何明文**。
3. **两个密钥域完全分离**（这是关键设计）：
   - ``relay_token``：用于向中继证明身份，中继知道它。它只能用来配对，不能解密。
   - ``password``：仅用于派生 E2E 会话密钥，**从不发给中继**。
   所以即使中继服务器被入侵，攻击者拿到的也只是密文。

nonce 拼接顺序固定为「Agent 的 nonce + Viewer 的 nonce」，
避免两端拼接顺序不同导致派生出的会话密钥不一致。
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Scrypt 参数：N=2^14, r=8, p=1 -> 约 16MB 内存、几十毫秒。
# 取值受 cryptography 内部的 OpenSSL maxmem 限制，2^15 会踩到上限。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

# PSK 派生用的固定盐。这里不需要随机盐：随机盐必须双方同步，
# 而真正的一次性随机性由后面的会话 nonce 提供。
PSK_SALT = b"alspd-desk/psk/v1"

SESSION_INFO_PREFIX = b"alspd-desk/e2e/v1|"
GCM_NONCE_LEN = 12
GCM_TAG_LEN = 16
SEQ_LEN = 4
KEY_LEN = 32


class SequenceError(Exception):
    """收到的包序号不连续。"""


def new_nonce(n: int = 16) -> bytes:
    """生成一次性随机 nonce。"""
    return os.urandom(n)


def derive_psk(password: str) -> bytes:
    """密码 -> 32 字节 PSK（Scrypt 拉伸）。"""
    if not password:
        raise ValueError("密码不能为空")
    kdf = Scrypt(salt=PSK_SALT, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


def derive_session_key(psk: bytes, agent_nonce: bytes, viewer_nonce: bytes) -> bytes:
    """PSK + 双方 nonce -> 会话密钥。

    拼接顺序**固定**为 agent_nonce + viewer_nonce，两端必须一致。
    """
    info = SESSION_INFO_PREFIX + agent_nonce + b"|" + viewer_nonce
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=None, info=info)
    return hkdf.derive(psk)


class SecureChannel:
    """单向有序通道上的 AES-256-GCM 加解密。

    一个实例同时维护发送和接收两个独立计数器：
        A.tx <-> B.rx   且   B.tx <-> A.rx
    因为底层是 WebSocket(TCP)，消息有序且不丢，所以隐式计数器是安全的；
    同时在密文前带上明文序号，便于发现错位并给出清晰报错。

    密文布局：``[4 字节序号][AES-GCM 密文(含 16 字节 tag)]``
    """

    def __init__(self, key: bytes):
        if len(key) != KEY_LEN:
            raise ValueError("会话密钥长度必须是 %d 字节，实际 %d" % (KEY_LEN, len(key)))
        self._aead = AESGCM(key)
        self._tx_seq = 0
        self._rx_seq = 0

    @property
    def tx_seq(self) -> int:
        return self._tx_seq

    @property
    def rx_seq(self) -> int:
        return self._rx_seq

    @staticmethod
    def _nonce(seq: int) -> bytes:
        return seq.to_bytes(GCM_NONCE_LEN, "big")

    def encrypt(self, payload: bytes, aad: bytes = b"") -> bytes:
        seq = self._tx_seq
        blob = self._aead.encrypt(self._nonce(seq), payload, aad)
        self._tx_seq += 1
        return seq.to_bytes(SEQ_LEN, "big") + blob

    def decrypt(self, blob: bytes, aad: bytes = b"") -> bytes:
        if len(blob) < SEQ_LEN + GCM_TAG_LEN:
            raise ValueError("密文过短：%d 字节" % len(blob))
        seq = int.from_bytes(blob[:SEQ_LEN], "big")
        if seq != self._rx_seq:
            raise SequenceError("序号不连续：期望 %d，收到 %d" % (self._rx_seq, seq))
        try:
            plain = self._aead.decrypt(self._nonce(seq), blob[SEQ_LEN:], aad)
        except InvalidTag:
            raise ValueError("解密失败（认证标签不匹配）—— 会话密钥不一致或数据被篡改") from None
        self._rx_seq += 1
        return plain

    def reset(self) -> None:
        self._tx_seq = 0
        self._rx_seq = 0
