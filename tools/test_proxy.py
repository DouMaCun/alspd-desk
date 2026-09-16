#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_proxy.py —— 代理探测与代理传输的测试

为什么需要代理
--------------
公司的网络策略里「必须经代理才能出网」非常常见。只做直连的话，
遇到这种环境整个方案就直接失败 —— 而实际上经代理是能通的。

本测试做两件事
--------------
1. **单元验证**代理地址解析与候选选择（含 IE 注册表那种 ``http=..;https=..;socks=..`` 写法）
2. **起两个假代理做真实转发**（HTTP CONNECT + SOCKS5），
   让一端直连、另一端经代理连到真实中继，验证**加密通道能真的穿过代理**。

第 2 点才是关键 —— 只测「解析对不对」是没法证明代理路径真的能用的。

用法
----
    python tools/test_proxy.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import config as config_mod          # noqa: E402
from common import crypto, protocol, proxy, wssession  # noqa: E402
from common.console import setup_console         # noqa: E402

setup_console()

try:
    from websockets.asyncio.server import serve
except ImportError:
    print("需要 websockets 库")
    raise

import relay.server as relay                     # noqa: E402

RELAY_PORT = 18546
# 「直连必然失败、只有经代理才能到达」的目标。这样才真正验证了代理路径可用，
# 而不是被「直连恰好也能通」掩盖。
#
# 为什么用两个不同形式的目标：
#   * 主机名 —— 测 HTTP CONNECT。HTTP 代理自己解析域名，用假域名很方便。
#   * IP     —— 测 SOCKS5。**python-socks 会在本地解析主机名**，
#               用假域名会先卡在本地 DNS 上，测不到 SOCKS 隧道本身。
#               顺带说明一个真实约束：走 SOCKS 时中继地址必须是本机能解析的
#               （IP 或真实域名）。用户只有 IP，所以实践中不受影响。
UNREACHABLE_HOSTNAME = "relay.invalid"
UNREACHABLE_IP = "127.0.0.2"      # 整个 127.0.0.0/8 都是环回；没监听就立刻 refused
UPSTREAM = {
    UNREACHABLE_HOSTNAME: ("127.0.0.1", RELAY_PORT),
    UNREACHABLE_IP: ("127.0.0.1", RELAY_PORT),
}
HTTP_PROXY_PORT = 18547
SOCKS_PROXY_PORT = 18548
ROOM = "proxy-test-room"
TOKEN = "proxy-test-token-0123456789abcdef"
PASSWORD = "proxy-test-password-0123456789"

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


def quiet(_msg):
    pass


# ============================================================ 假代理

async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def http_connect_proxy_handler(reader, writer):
    """最小可用的 HTTP CONNECT 代理。只实现 CONNECT，够测我们的场景了。"""
    stats["http_connects"] += 1
    try:
        line = await reader.readline()
        if not line:
            return
        # 读完剩余请求头
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
        parts = line.decode("latin-1").split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        hostport = parts[1]
        host, _, port = hostport.rpartition(":")
        if not host or not port.isdigit():
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        real_host, real_port = UPSTREAM.get(host, (host, int(port)))
        try:
            r2, w2 = await asyncio.open_connection(real_host, real_port)
        except Exception:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return
        stats["http_tunnel_ok"] += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, w2), _pipe(r2, writer))
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def socks5_proxy_handler(reader, writer):
    """最小可用的 SOCKS5 代理（无认证，仅 CONNECT）。"""
    stats["socks_connects"] += 1
    try:
        hdr = await reader.readexactly(2)
        ver, nmethods = hdr[0], hdr[1]
        methods = await reader.readexactly(nmethods) if nmethods else b""
        if ver != 5 or 0x00 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")          # 无需认证
        await writer.drain()

        req = await reader.readexactly(4)
        _ver, cmd, _rsv, atyp = req
        if atyp == 1:
            addr = ".".join(str(b) for b in await reader.readexactly(4))
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            addr = (await reader.readexactly(ln)).decode("latin-1")
        elif atyp == 4:
            import ipaddress
            addr = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)   # 地址类型不支持
            await writer.drain()
            return
        port = int.from_bytes(await reader.readexactly(2), "big")

        if cmd != 1:                        # 只要 CONNECT
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return
        real_host, real_port = UPSTREAM.get(addr, (addr, port))
        try:
            r2, w2 = await asyncio.open_connection(real_host, real_port)
        except Exception:
            writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)   # 连接被拒
            await writer.drain()
            return
        stats["socks_tunnel_ok"] += 1
        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)        # 成功
        await writer.drain()
        await asyncio.gather(_pipe(reader, w2), _pipe(r2, writer))
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


stats = {"http_connects": 0, "http_tunnel_ok": 0,
         "socks_connects": 0, "socks_tunnel_ok": 0}


# ============================================================ 单元测试

def test_parsing():
    print("[1] 代理地址解析")
    check("host:port 自动补 http://",
          proxy.normalize("127.0.0.1:8080") == "http://127.0.0.1:8080",
          proxy.normalize("127.0.0.1:8080"))
    check("socks5:// 保持", proxy.normalize("socks5://127.0.0.1:10808") == "socks5://127.0.0.1:10808")
    check("socks5h 归一为 socks5",
          proxy.normalize("socks5h://127.0.0.1:1080") == "socks5://127.0.0.1:1080")
    check("带用户名密码的地址保留",
          proxy.normalize("http://u:p@10.0.0.1:3128") == "http://u:p@10.0.0.1:3128")
    check("非法输入返回 None", proxy.normalize("not a proxy") is None)
    check("空串返回 None", proxy.normalize("") is None)
    check("不支持的 scheme 返回 None", proxy.normalize("ftp://127.0.0.1:21") is None)
    print()

    print("[2] IE 代理设置的两种写法")
    single = proxy.parse_wininet_proxy("127.0.0.1:10809")
    check("单代理写法解析出 1 条", len(single) == 1 and single[0].uri == "http://127.0.0.1:10809",
          "%s" % [s.uri for s in single])

    per_proto = proxy.parse_wininet_proxy("http=127.0.0.1:10809;https=127.0.0.1:10809;socks=127.0.0.1:10808")
    uris = [s.uri for s in per_proto]
    check("按协议写法解析出多条", len(per_proto) >= 2, "%s" % uris)
    check("https 项被识别（我们的流量就是 wss）",
          any(u.startswith("http") for u in uris), "%s" % uris)
    check("socks 项被识别并补上 socks5://",
          "socks5://127.0.0.1:10808" in uris, "%s" % uris)

    check("空值返回空列表", proxy.parse_wininet_proxy("") == [])
    check("乱码返回空列表", proxy.parse_wininet_proxy(";;;===") == [])
    print()


def test_candidates():
    print("[3] 候选选择策略")
    direct_only = proxy.candidates("direct")
    check("direct 模式只返回直连",
          len(direct_only) == 1 and direct_only[0][1] is None, "%s" % direct_only)

    manual = proxy.candidates("socks5://127.0.0.1:10808")
    check("手动指定时代理排在直连之后",
          len(manual) == 2 and manual[0][1] is None and manual[1][1] == "socks5://127.0.0.1:10808",
          "%s" % manual)

    auto = proxy.candidates("auto")
    check("auto 模式第一项恒为直连", auto[0][1] is None, "%s" % auto[:2])

    bad = proxy.candidates("这不是一个代理")
    check("配置写错时退回 auto 而不是直接失败", len(bad) >= 1 and bad[0][1] is None,
          "候选 %d 条" % len(bad))
    print()


# ============================================================ 端到端

def build_cfg(proxy_mode: str, host: str = "127.0.0.1") -> config_mod.AppConfig:
    cfg = config_mod.AppConfig()
    cfg.common.relay_host = host
    cfg.common.relay_ports = [RELAY_PORT]
    cfg.common.room = ROOM
    cfg.common.relay_token = TOKEN
    cfg.common.password = PASSWORD
    cfg.common.proxy = proxy_mode
    cfg.tls.enabled = False
    return cfg


async def pair_and_verify(agent_proxy: str, viewer_proxy: str, label: str,
                          host: str = "127.0.0.1"):
    """两端分别用指定代理连到中继，配对并做一次加密往返。

    注意：**两端都必须能到达中继**。测 unreachable host 时如果只给一端配代理，
    另一端会永远连不上，配对就卡住了。
    """
    cfg_a = build_cfg(agent_proxy, host)
    cfg_v = build_cfg(viewer_proxy, host)

    task_a = asyncio.create_task(wssession.connect(
        cfg_a, protocol.ROLE_AGENT,
        {"screen": {"width": 640, "height": 448}, "readonly": True}, log=quiet))
    task_v = asyncio.create_task(wssession.connect(
        cfg_v, protocol.ROLE_VIEWER, {"viewer": {}}, log=quiet))

    sess_a = sess_v = None
    try:
        sess_a, sess_v = await asyncio.wait_for(
            asyncio.gather(task_a, task_v), timeout=50)
    except Exception as e:
        for t in (task_a, task_v):
            t.cancel()
        await asyncio.gather(task_a, task_v, return_exceptions=True)
        check("%s：配对成功" % label, False, "%s: %s" % (type(e).__name__, str(e)[:90]))
        return False
    finally:
        # 兜底：任何情况下都不留悬挂的连接任务，否则事件循环关不掉
        for t in (task_a, task_v):
            if not t.done():
                t.cancel()

    ok = check("%s：配对成功" % label, True,
               "agent 经 %s / viewer 经 %s" % (agent_proxy, viewer_proxy))
    try:
        # 加密往返：证明通道确实能承载数据，而不是只有 TCP 连上了
        await sess_a.send_ctrl(protocol.CTRL_PING, ts=12345)
        got = None
        deadline = time.time() + 10
        while time.time() < deadline:
            mtype, payload = await asyncio.wait_for(sess_v.recv_peer_message(), timeout=6)
            if mtype == protocol.MSG_CTRL:
                got = protocol.parse_ctrl(payload)
                break
        ok = check("%s：加密消息能穿过代理" % label,
                   got is not None and got.get("t") == protocol.CTRL_PING
                   and got.get("ts") == 12345,
                   "收到 %s" % got) and ok
    except Exception as e:
        check("%s：加密消息能穿过代理" % label, False, "%s: %s" % (type(e).__name__, e))
        ok = False
    finally:
        for s in (sess_a, sess_v):
            if s is not None:
                await s.close()
    return ok


async def main() -> int:
    print()
    print("=" * 74)
    print("  ALSPD-DESK  代理探测与代理传输测试")
    print("=" * 74)

    test_parsing()
    test_candidates()

    print("[4] 真实中继 + 假代理（HTTP CONNECT / SOCKS5）")
    relay.expected_token = TOKEN
    relay.expected_room = ROOM
    relay.LOG.setLevel(40)

    async with serve(relay.handle, "127.0.0.1", RELAY_PORT, max_size=16 * 1024 * 1024,
                     ping_interval=None, compression=None):
        http_srv = await asyncio.start_server(http_connect_proxy_handler, "127.0.0.1",
                                              HTTP_PROXY_PORT)
        socks_srv = await asyncio.start_server(socks5_proxy_handler, "127.0.0.1",
                                               SOCKS_PROXY_PORT)
        print("  中继 :%d  假HTTP代理 :%d  假SOCKS5代理 :%d"
              % (RELAY_PORT, HTTP_PROXY_PORT, SOCKS_PROXY_PORT))
        print()
        async with http_srv, socks_srv:
            # ---- 基准：目标是 127.0.0.1，直连就能通 ----
            await pair_and_verify("direct", "direct", "两端直连（目标可达）")
            await asyncio.sleep(0.5)

            # ---- 关键设计：目标换成「直连必然失败」的主机 ----
            # 否则直连先成功，代理根本不会被尝试，测试就变成了自我安慰。
            print()
            print("  目标改为 %s —— 直连必然失败，只有经代理才能到达中继" % UNREACHABLE_IP)
            cfg_direct = build_cfg("direct", UNREACHABLE_IP)
            try:
                await asyncio.wait_for(
                    wssession.connect(cfg_direct, protocol.ROLE_AGENT,
                                      {"screen": {}}, log=quiet), timeout=25)
                check("对照组：直连 %s 应当失败" % UNREACHABLE_IP, False,
                      "居然连上了，说明这个对照无效")
            except asyncio.TimeoutError:
                check("对照组：直连 %s 应当失败" % UNREACHABLE_IP, False, "超时")
            except Exception as e:
                check("对照组确认：直连 %s 确实不通" % UNREACHABLE_IP, True,
                      "%s" % type(e).__name__)
            await asyncio.sleep(0.5)

            # ---- 经 HTTP CONNECT 代理 ----
            n0 = stats["http_tunnel_ok"]
            await pair_and_verify("http://127.0.0.1:%d" % HTTP_PROXY_PORT,
                                  "http://127.0.0.1:%d" % HTTP_PROXY_PORT,
                                  "两端都经 HTTP CONNECT 代理", host=UNREACHABLE_HOSTNAME)
            check("确实走了 HTTP 代理隧道（直连不通，只可能来自代理）",
                  stats["http_tunnel_ok"] > n0,
                  "隧道建立 %d 次" % stats["http_tunnel_ok"])
            await asyncio.sleep(0.5)

            # ---- 经 SOCKS5 代理 ----
            n1 = stats["socks_tunnel_ok"]
            await pair_and_verify("socks5://127.0.0.1:%d" % SOCKS_PROXY_PORT,
                                  "socks5://127.0.0.1:%d" % SOCKS_PROXY_PORT,
                                  "两端都经 SOCKS5 代理", host=UNREACHABLE_IP)
            check("确实走了 SOCKS5 代理隧道",
                  stats["socks_tunnel_ok"] > n1,
                  "隧道建立 %d 次" % stats["socks_tunnel_ok"])
            await asyncio.sleep(0.5)

            # ---- 两端分别经不同代理 ----
            await pair_and_verify("socks5://127.0.0.1:%d" % SOCKS_PROXY_PORT,
                                  "http://127.0.0.1:%d" % HTTP_PROXY_PORT,
                                  "两端分别经不同代理", host=UNREACHABLE_IP)

            # ---- auto 模式：靠环境变量自动发现代理 ----
            print()
            print("[5] auto 模式的自愈能力")
            env_keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
                        "http_proxy", "ALL_PROXY", "all_proxy")
            old_env = {k: os.environ.get(k) for k in env_keys}
            try:
                for k in env_keys:
                    os.environ.pop(k, None)

                # 完全没有可用代理时：必须明确失败，不能假装成功
                cfg_auto = build_cfg("auto", UNREACHABLE_IP)
                try:
                    await asyncio.wait_for(
                        wssession.connect(cfg_auto, protocol.ROLE_AGENT,
                                          {"screen": {}}, log=quiet), timeout=40)
                    check("对照组：无可用代理时 auto 应当失败", False, "居然连上了")
                except asyncio.TimeoutError:
                    check("对照组：无可用代理时 auto 应当失败", False, "超时（不该这么慢）")
                except Exception as e:
                    check("对照组确认：无可用代理时 auto 明确失败，不会假装成功",
                          True, "%s" % type(e).__name__)

                # 把假代理塞进环境变量，auto 应当自动发现并使用它
                os.environ["HTTPS_PROXY"] = "http://127.0.0.1:%d" % HTTP_PROXY_PORT
                detected = [s.uri for s in proxy.detect_all()]
                check("auto 能自动探测到环境变量里的代理",
                      "http://127.0.0.1:%d" % HTTP_PROXY_PORT in detected,
                      "%s" % detected[:3])

                n2 = stats["http_tunnel_ok"]
                # auto 会自动探测到环境变量里的代理；两端都用 auto
                await pair_and_verify("auto", "auto", "auto 自动使用探测到的代理",
                                      host=UNREACHABLE_HOSTNAME)
                check("auto 模式下确实经代理建立了隧道（直连不通）",
                      stats["http_tunnel_ok"] > n2,
                      "隧道累计 %d 次" % stats["http_tunnel_ok"])
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  代理解析、候选策略、HTTP CONNECT 与 SOCKS5 传输均可用。")
        print("=" * 74)
        return 0
    print("  ❌ 有失败项：%d/%d 通过" % (passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 74)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
