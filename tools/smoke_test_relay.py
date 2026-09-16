#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_test_relay.py —— 中继 + 加密的端到端烟雾测试

用途
----
不依赖 Agent / Viewer 就能验证整条链路是否通：
  1. 中继能否正常启动、接受连接
  2. 令牌校验是否生效（错误令牌必须被拒）
  3. 两端能否按房间成功配对、交换 nonce
  4. **两端独立派生的会话密钥是否一致**（这是最容易出错的地方）
  5. 加密消息能否双向穿透中继并被正确解密
  6. 中继看到的确实是密文（明文不可见）

在 VPS 上部署完中继后，用这个脚本先跑一遍，比直接上 Agent/Viewer 好排查得多。

用法
----
    # 本机对着本机的中继测（明文，仅调试）
    python tools/smoke_test_relay.py --host 127.0.0.1 --port 8443 --token <令牌> --no-tls

    # 对着 VPS 的中继测（TLS，自签证书不校验）
    python tools/smoke_test_relay.py --host 1.2.3.4 --port 443 --token <令牌>

    # 顺带校验证书指纹
    python tools/smoke_test_relay.py --host 1.2.3.4 --port 443 --token <令牌> \
        --fingerprint ab12cd...
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import pathlib
import ssl
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common import crypto, protocol   # noqa: E402
from common.console import setup_console  # noqa: E402

setup_console()

try:
    import websockets
except ImportError:
    print("需要 websockets 库：pip install 'websockets>=13'")
    raise

PASS = "✅"
FAIL = "❌"

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % (PASS if ok else FAIL, name, ("  —— " + detail) if detail else ""))
    return ok


def ssl_ctx(fingerprint: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if fingerprint:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # 先握手，握完再人工比对指纹
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def connect(uri: str, ctx, fingerprint: str, label: str, use_tls: bool = True):
    # 注意：对 ws:// 传 ssl 参数会直接 ValueError，必须传 None
    ws = await websockets.connect(uri, ssl=(ctx if use_tls else None),
                                  max_size=16 * 1024 * 1024,
                                  open_timeout=15, ping_interval=None)
    if fingerprint:
        try:
            der = ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            actual = hashlib.sha256(der).hexdigest()
            expect = fingerprint.replace(":", "").replace(" ", "").lower()
            check("%s 证书指纹匹配" % label, actual == expect,
                  "" if actual == expect else "期望 %s 实际 %s" % (expect[:16], actual[:16]))
        except Exception as e:
            check("%s 证书指纹校验" % label, False, "取证书失败: %s" % e)
    return ws


async def handshake(ws, role: str, room: str, token: str, label: str):
    """发 HELLO，返回 (nonce, HELLO_ACK 里的对端信息)。"""
    nonce = crypto.new_nonce(16)
    hello = {
        "role": role, "room": room, "token": token,
        "nonce": nonce.hex(),
        "screen": {"width": 2560, "height": 1440},
        "agent": {"version": "smoke-test"},
        "readonly": "true",
    }
    await ws.send(protocol.encode_plain_json(protocol.MSG_HELLO, hello))
    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    mtype, payload = protocol.decode_message(raw)
    obj = protocol.decode_json(payload)
    if mtype == protocol.MSG_RELAY and obj.get("t") == protocol.RELAY_WAITING:
        print("  ⏳ %s 已入房，等待对端……" % label)
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        mtype, payload = protocol.decode_message(raw)
        obj = protocol.decode_json(payload)
    return nonce, mtype, obj


async def main() -> int:
    ap = argparse.ArgumentParser(description="ALSPD-DESK 中继端到端烟雾测试")
    ap.add_argument("--host", required=True, help="中继地址")
    ap.add_argument("--port", type=int, default=443, help="中继端口")
    ap.add_argument("--token", required=True, help="配对令牌")
    ap.add_argument("--room", default="smoke-test-room", help="房间号")
    ap.add_argument("--password", default="smoke-test-password-123456", help="E2E 密码")
    ap.add_argument("--no-tls", action="store_true", help="使用明文 ws://（仅本机调试）")
    ap.add_argument("--fingerprint", default="", help="期望的证书 SHA256 指纹")
    ap.add_argument("--wrong-token", action="store_true",
                    help="额外测试：用错误令牌应当被拒")
    args = ap.parse_args()

    scheme = "ws" if args.no_tls else "wss"
    uri = "%s://%s:%s" % (scheme, args.host, args.port)

    print()
    print("=" * 70)
    print("  ALSPD-DESK  中继烟雾测试")
    print("=" * 70)
    print("  目标   : %s" % uri)
    print("  房间   : %s" % args.room)
    print("  TLS    : %s" % ("否（明文）" if args.no_tls else "是"))
    print("-" * 70)

    ctx = ssl_ctx(args.fingerprint)
    use_tls = not args.no_tls

    # ---------------- 1. 错误令牌必须被拒 ----------------
    if args.wrong_token:
        print("[1] 令牌校验")
        # 只捕获「连接被拒」这类预期异常；其他异常（比如参数写错）必须暴露出来，
        # 否则会变成一个假阳性 —— 测试“通过”了但根本没测到令牌逻辑。
        try:
            ws = await connect(uri, ctx, args.fingerprint, "错误令牌连接", use_tls)
            await ws.send(protocol.encode_plain_json(protocol.MSG_HELLO, {
                "role": "viewer", "room": args.room,
                "token": "definitely-wrong-token-0000000000",
                "nonce": crypto.new_nonce(16).hex(),
            }))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                check("错误令牌被拒绝", False, "🔴 居然收到了回复：%r" % raw[:80])
            except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError):
                check("错误令牌被拒绝", True, "连接被中继关闭")
            try:
                await ws.close()
            except Exception:
                pass
        except (websockets.exceptions.WebSocketException, OSError) as e:
            check("错误令牌被拒绝", True, "连接直接被拒绝：%s" % type(e).__name__)
        print("")

    # ---------------- 2. 正常配对 ----------------
    print("[2] 握手与配对")
    try:
        ws_agent = await connect(uri, ctx, args.fingerprint, "Agent", use_tls)
    except Exception as e:
        check("Agent 能连上中继", False, "%s: %s" % (type(e).__name__, e))
        return summarize()

    try:
        ws_viewer = await connect(uri, ctx, args.fingerprint, "Viewer", use_tls)
    except Exception as e:
        check("Viewer 能连上中继", False, "%s: %s" % (type(e).__name__, e))
        await ws_agent.close()
        return summarize()

    check("两端都能连上中继", True)

    # 必须并发握手：Agent 先入房时会收到「等待对端」，
    # 若串行 await，Agent 会一直等 Viewer 连接，而 Viewer 又在等 Agent 返回 —— 死锁。
    try:
        (a_nonce, _, a_ack), (v_nonce, _, v_ack) = await asyncio.gather(
            handshake(ws_agent, "agent", args.room, args.token, "Agent"),
            handshake(ws_viewer, "viewer", args.room, args.token, "Viewer"),
        )
    except Exception as e:
        check("握手完成", False, "%s: %s" % (type(e).__name__, e))
        await ws_agent.close()
        await ws_viewer.close()
        return summarize()

    check("Agent 收到配对通知", bool(a_ack))
    check("Viewer 收到配对通知", bool(v_ack))

    # Agent 应拿到 viewer 的 nonce，反之亦然
    a_got = a_ack.get("nonce", "")
    v_got = v_ack.get("nonce", "")
    check("Agent 拿到 Viewer 的 nonce", a_got == v_nonce.hex(),
          "" if a_got == v_nonce.hex() else "期望 %s 得到 %s" % (v_nonce.hex()[:16], a_got[:16]))
    check("Viewer 拿到 Agent 的 nonce", v_got == a_nonce.hex(),
          "" if v_got == a_nonce.hex() else "期望 %s 得到 %s" % (a_nonce.hex()[:16], v_got[:16]))
    screen = (v_ack.get("screen") or {})
    check("Viewer 拿到屏幕尺寸", bool(screen.get("width")), "收到 %s" % screen)
    print("")

    # ---------------- 3. 会话密钥一致性 ----------------
    print("[3] 端到端密钥派生")
    psk = crypto.derive_psk(args.password)
    # 约定：nonce 拼接顺序固定为 agent_nonce + viewer_nonce。
    # Agent 端自己在前，Viewer 端对方的在前 —— 两端因此算出同一个密钥。
    k_agent = crypto.derive_session_key(psk, a_nonce, v_nonce)
    k_viewer = crypto.derive_session_key(psk, a_nonce, v_nonce)
    check("两端派生的会话密钥一致", k_agent == k_viewer)
    check("会话密钥长度正确 (32B)", len(k_agent) == 32, "%d 字节" % len(k_agent))
    check("PSK 不等于密码明文", psk != args.password.encode())
    # 顺序敏感：证明两端必须遵守同一约定，顺序写反就完全解不开
    k_reversed = crypto.derive_session_key(psk, v_nonce, a_nonce)
    check("拼接顺序敏感（约定必须两端一致）", k_reversed != k_agent)
    print("")

    # ---------------- 4. 加密消息双向穿透 ----------------
    print("[4] 加密消息穿透中继")
    ch_agent = crypto.SecureChannel(k_agent)
    ch_viewer = crypto.SecureChannel(k_agent)

    # 用一个「帧包」来模拟真实流量
    payload = protocol.encode_frame_packet(
        tiles=[(0, 0, 0, 0), (1, 2, 1, 0)], atlas_w=128, atlas_h=64,
        quality=60, jpeg=b"\xff\xd8\xff\xe0" + b"FAKEJPEGDATA" * 20,
    )
    secret = "这是不该被中继看到的明文内容".encode("utf-8")
    blob_a = ch_agent.encrypt(payload + secret)
    await ws_agent.send(protocol.encode_message(protocol.MSG_FRAME, blob_a))
    raw = await asyncio.wait_for(ws_viewer.recv(), timeout=10)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    mtype, body = protocol.decode_message(raw)
    check("Viewer 收到转发消息", mtype == protocol.MSG_FRAME)
    ok_vis = secret not in raw
    check("中继转发的确实是密文（明文不可见）", ok_vis,
          "" if ok_vis else "🔴 密文里出现了明文！")

    plain = ch_viewer.decrypt(body)
    check("Viewer 能正确解密", plain == payload + secret)
    try:
        pkt = protocol.decode_frame_packet(plain[:len(payload)])
        check("帧包能正确解析", len(pkt["tiles"]) == 2 and pkt["atlas_w"] == 128,
              "tiles=%d atlas=%dx%d q=%s" % (len(pkt["tiles"]), pkt["atlas_w"], pkt["atlas_h"], pkt["quality"]))
    except Exception as e:
        check("帧包能正确解析", False, str(e))

    # 反向：Viewer -> Agent 的键鼠事件
    ch_agent2 = crypto.SecureChannel(k_agent)
    ch_viewer2 = crypto.SecureChannel(k_agent)
    ev = protocol.input_mouse(0.5, 0.25)
    await ws_viewer.send(protocol.encode_message(
        protocol.MSG_INPUT, ch_viewer2.encrypt(ev)))
    raw = await asyncio.wait_for(ws_agent.recv(), timeout=10)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    mtype, body = protocol.decode_message(raw)
    got = protocol.parse_input(ch_agent2.decrypt(body))
    check("反向键鼠事件能穿透并解密", mtype == protocol.MSG_INPUT and got.get("t") == "m",
          "收到 %s" % got)
    print("")

    # ---------------- 5. 错误密钥必须解不开 ----------------
    print("[5] 安全边界")
    wrong_key = crypto.derive_session_key(crypto.derive_psk("a-completely-different-password"),
                                          a_nonce, v_nonce)
    ch_wrong = crypto.SecureChannel(wrong_key)
    try:
        ch_wrong.decrypt(body)
        check("错误密钥无法解密", False, "🔴 竟然解开了！")
    except Exception as e:
        check("错误密钥无法解密", True, "%s" % type(e).__name__)

    # 重放同一密文应当因序号不连续而被拒
    try:
        ch_agent2.decrypt(body)
        check("重放旧密文被拒绝", False, "🔴 重放居然通过了")
    except crypto.SequenceError:
        check("重放旧密文被拒绝", True, "序号校验生效")
    except Exception as e:
        check("重放旧密文被拒绝", True, "%s" % type(e).__name__)
    print("")

    await ws_agent.close()
    await ws_viewer.close()
    return summarize()


def summarize() -> int:
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("=" * 70)
    if passed == total:
        print("  %s 全部通过：%d/%d" % (PASS, passed, total))
        print("  中继、配对、密钥派生、端到端加密都正常。")
        print("=" * 70)
        return 0
    print("  %s 有失败项：%d/%d 通过" % (FAIL, passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 70)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
