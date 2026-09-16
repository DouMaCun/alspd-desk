# -*- coding: utf-8 -*-
"""代理探测与选择。

为什么需要这一层
----------------
公司的网络策略里，「必须经代理才能出网」非常常见。如果只做直连，
一旦遇到这种环境，整个方案就直接失败 —— 而实际上经代理是能通的。

``websockets`` 自己只认**环境变量**（HTTP_PROXY / HTTPS_PROXY），
**不会读 Windows 的 IE 代理设置**（注册表）和 WinHTTP 代理。而公司电脑上
配的代理通常恰恰就在注册表里。所以这里自己探测。

它支持哪几种代理
----------------
``websockets`` 原生支持两类（无需额外代码）：

* ``http://`` / ``https://`` —— HTTP CONNECT 隧道
* ``socks4://`` / ``socks4a://`` / ``socks5://`` / ``socks5h://`` —— SOCKS
  （SOCKS 需要 ``python-socks``，已在 requirements 里）

选择策略（``common.proxy`` 配置项）
-----------------------------------
* ``"auto"``（默认）—— 先直连；全部端口都不通时，再依次尝试探测到的代理
* ``"direct"`` —— 只直连，完全不用代理
* ``"<uri>"`` —— 先直连；不通时用这个指定代理，例如 ``socks5://127.0.0.1:10808``

**先直连**是有意的：直连不过任何第三方服务器，链路最短、延迟最低；
代理只作为兜底。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

IS_WIN = sys.platform == "win32"

# 常见本地代理客户端的默认端口，用于在没有明确配置时给出「值得一试」的候选。
# 例如 v2rayN 默认 SOCKS 10808 / HTTP 10809，Clash 默认 7890。
# 注意：这些只是**候选**，连不上就会被跳过，不会有什么副作用。
WELL_KNOWN_LOCAL = [
    ("socks5://127.0.0.1:10808", "常见 SOCKS5 端口（v2rayN 默认）"),
    ("http://127.0.0.1:10809", "常见 HTTP 端口（v2rayN 默认）"),
    ("http://127.0.0.1:7890", "常见 HTTP 端口（Clash 默认）"),
    ("socks5://127.0.0.1:7890", "常见 SOCKS5 端口（Clash 默认）"),
    ("http://127.0.0.1:8080", "常见本地 HTTP 代理端口"),
]

_HOST_PORT = re.compile(r"^([a-zA-Z0-9._\-]+):(\d{1,5})$")


@dataclass(frozen=True)
class ProxySpec:
    uri: str
    source: str

    def describe(self) -> str:
        return "%s  [%s]" % (self.uri, self.source)

    def __str__(self) -> str:
        return self.describe()


def normalize(url: str) -> Optional[str]:
    """把用户/注册表里的写法规整成 websockets 能接受的 URI。"""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        # 没写 scheme 的按 HTTP 代理处理（注册表里通常就是 host:port）
        url = "http://" + url
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme in ("socks", "socks5h"):
        scheme = "socks5"
    if scheme not in ("http", "https", "socks4", "socks4a", "socks5"):
        return None
    # 校验 host:port —— 注意要先剥掉 "user:pass@" 这种认证信息，
    # 否则里面的冒号会让 host:port 的校验失败（公司代理经常带认证）
    authority = rest.split("/")[0]
    hostpart = authority.rsplit("@", 1)[-1]
    if not _HOST_PORT.match(hostpart):
        return None
    return "%s://%s" % (scheme, rest)


def parse_wininet_proxy(server: str) -> List[ProxySpec]:
    """解析 IE 代理设置里的 ``ProxyServer`` 值。

    它有两种写法：

    * ``host:port`` —— 所有协议共用一个代理
    * ``http=h:p;https=h:p;ftp=h:p;socks=h:p`` —— 按协议分别指定

    优先取 https（我们的流量就是 wss），其次 http，最后 socks。
    """
    if not server:
        return []
    out: List[ProxySpec] = []
    server = server.strip()

    if "=" not in server:
        uri = normalize(server)
        if uri:
            out.append(ProxySpec(uri, "IE 代理设置（所有协议）"))
        return out

    per_proto = {}
    for part in server.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        proto, _, value = part.partition("=")
        per_proto[proto.strip().lower()] = value.strip()

    for proto, label in (("https", "IE 代理设置（https）"),
                         ("http", "IE 代理设置（http）"),
                         ("socks", "IE 代理设置（socks）")):
        if proto in per_proto:
            value = per_proto[proto]
            if proto == "socks":
                value = "socks5://" + value
            uri = normalize(value)
            if uri:
                out.append(ProxySpec(uri, label))
    return out


def detect_env() -> List[ProxySpec]:
    """环境变量里的代理（websockets 默认也会读，这里显式列出来便于报告）。"""
    out: List[ProxySpec] = []
    seen = set()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if not value:
            continue
        uri = normalize(value)
        if uri and uri not in seen:
            seen.add(uri)
            out.append(ProxySpec(uri, "环境变量 %s" % name))
    return out


def detect_registry() -> List[ProxySpec]:
    """Windows IE 代理设置（注册表）。这是公司电脑上最常见的一处。"""
    if not IS_WIN:
        return []
    try:
        import winreg
    except ImportError:
        return []
    out: List[ProxySpec] = []
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            def get(name, default=None):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default

            enabled = get("ProxyEnable", 0)
            server = get("ProxyServer", "") or ""
            auto_url = get("AutoConfigURL", "") or ""
            if enabled and server:
                out.extend(parse_wininet_proxy(server))
            if auto_url:
                # PAC 脚本我们无法直接求值；只提示存在，便于排查
                out.append(ProxySpec("", "检测到 PAC 脚本：%s（本工具不解析 PAC，"
                                         "如必须走 PAC 请手动指定代理地址）" % auto_url))
    except OSError:
        pass
    return [s for s in out if s.uri]


def detect_winhttp() -> List[ProxySpec]:
    """WinHTTP 代理（netsh winhttp show proxy）。有些程序只认这一处。"""
    if not IS_WIN:
        return []
    try:
        proc = subprocess.run(["netsh", "winhttp", "show", "proxy"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    raw = proc.stdout or b""
    text = None
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        return []
    if "Direct access" in text or "直接访问" in text or "no proxy" in text.lower():
        return []
    out: List[ProxySpec] = []
    for line in text.splitlines():
        # 中英文 Windows 的标签不同，所以直接按 host:port 的模式找，避免依赖文案
        for m in re.finditer(r"([a-zA-Z0-9._\-]+:\d{1,5})", line):
            uri = normalize(m.group(1))
            if uri:
                out.append(ProxySpec(uri, "WinHTTP 代理设置"))
    return out


def detect_all(include_well_known: bool = True) -> List[ProxySpec]:
    """汇总所有探测到的代理，去重并保持优先级：环境变量 > 注册表 > WinHTTP > 常见端口。"""
    out: List[ProxySpec] = []
    seen = set()
    for spec in (detect_env() + detect_registry() + detect_winhttp()):
        if spec.uri and spec.uri not in seen:
            seen.add(spec.uri)
            out.append(spec)
    if include_well_known and not out:
        # 一处都没探测到：给几个常见本地代理端口做「值得一试」的兜底。
        # 只在完全没探测到时才加，避免无谓的尝试拖慢连接。
        for uri, why in WELL_KNOWN_LOCAL:
            if uri not in seen:
                seen.add(uri)
                out.append(ProxySpec(uri, why + "（未探测到任何代理配置，仅为候选）"))
    return out


def candidates(proxy_cfg: str) -> List[tuple]:
    """按配置算出「传输候选」列表，返回 ``[(描述, proxy_uri 或 None)]``。

    直连永远排第一 —— 它不经任何第三方服务器，链路最短。
    """
    mode = (proxy_cfg or "auto").strip()
    direct = ("直连（不经代理）", None)

    if mode.lower() == "direct":
        return [direct]
    if mode.lower() == "auto" or not mode:
        return [direct] + [(s.describe(), s.uri) for s in detect_all()]

    uri = normalize(mode)
    if not uri:
        # 配错了就退回 auto，而不是直接失败 —— 连通性优先
        return [direct] + [(s.describe(), s.uri) for s in detect_all()]
    return [direct, ("指定的代理 %s" % mode, uri)]
