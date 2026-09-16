#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_server.py —— Phase 0a 侦察工具（VPS 侧）

用途
----
配合 tools/probe_agent.py 使用。在公司电脑上运行 agent，验证：
  1. 未签名的自建 exe 能否在那台机器上运行（能否不被 EDR 干掉）
  2. 能否连出公司网络
  3. VPS 的哪些端口能连通
  4. TLS 是否被公司中间人解密

本脚本在 VPS 上接收并显示这些侦察结果。

特点
----
* 只用 Python 标准库 —— 可直接 scp 到 Debian VPS 上用 python3 运行，无需 pip install
* 同一条监听自动识别明文 / TLS（看首字节是否 0x16），两种模式都能测
* 需要 TLS 时用 openssl 现场生成自签证书，并打印 SHA256 指纹供 agent 比对
* 端口被占用（例如 22 上的 SSH）会明确提示并跳过，不影响其他端口

用法
----
    # 推荐：明文 + TLS 双测（端口 <1024 需要 root）
    sudo python3 probe_server.py --tls

    # 自定义端口
    sudo python3 probe_server.py --tls --ports 443,8443,8080

    # 复用已有证书
    python3 probe_server.py --tls --cert /etc/alspd/probe.crt --key /etc/alspd/probe.key

按 Ctrl-C 停止。
"""

import argparse
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time


def setup_console():
    """Windows 控制台默认 GBK，中文符号会 UnicodeEncodeError。统一切到 UTF-8。

    Linux（VPS）本来就是 UTF-8，这里只是无害的兜底。
    """
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

DEFAULT_PORTS = [443, 8443, 80, 8080, 22]
LOG_LOCK = threading.Lock()
LOG_FILE = None
REPORT_FILE = None

BANNER = r"""
+--------------------------------------------------------------------+
|  ALSPD-DESK  Phase 0a  侦察服务端 (VPS 侧)                          |
+--------------------------------------------------------------------+
"""


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg, level="INFO"):
    """带时间戳输出到控制台，同时落盘，便于事后回看。"""
    line = "[%s] [%-5s] %s" % (now_str(), level, msg)
    with LOG_LOCK:
        print(line, flush=True)
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass


def ensure_cert(cert_dir):
    """自签证书不存在就用 openssl 现场生成，返回 (cert_path, key_path, sha256_fp)。"""
    os.makedirs(cert_dir, exist_ok=True)
    cert = os.path.join(cert_dir, "probe.crt")
    key = os.path.join(cert_dir, "probe.key")

    if not (os.path.exists(cert) and os.path.exists(key)):
        log("未找到自签证书，用 openssl 生成 -> %s" % cert)
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", cert,
                    "-days", "3650",
                    "-subj", "/CN=alspd-desk-probe",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log("找不到 openssl 命令，请先安装：apt install -y openssl", "ERROR")
            raise SystemExit(1)
        except subprocess.CalledProcessError as e:
            log("openssl 生成证书失败：%s" % e, "ERROR")
            raise SystemExit(1)

    with open(cert, "r", encoding="utf-8") as f:
        pem = f.read()
    der = ssl.PEM_cert_to_DER_cert(pem)
    fp = hashlib.sha256(der).hexdigest()
    return cert, key, fp


def format_fp(fp):
    """把 64 位十六进制指纹格式化成 XX:XX:... 便于人工比对。"""
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


def save_report(payload, peer, port, mode):
    if not REPORT_FILE:
        return
    try:
        with open(REPORT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"received_at": now_str(), "peer": peer, "port": port, "mode": mode, "payload": payload},
                ensure_ascii=False,
            ) + "\n")
    except OSError:
        pass


def show_report(payload, peer, port, mode):
    """把 agent 发来的完整侦察报告醒目地打印出来。"""
    log("=" * 70)
    log("收到完整侦察报告")
    log("  来源  : %s" % peer)
    log("  通道  : 端口 %s / %s" % (port, mode))
    log("-" * 70)

    order = [
        ("env", "本机环境"),
        ("proxy", "代理设置"),
        ("dns", "DNS 解析"),
        ("egress", "出网与 TLS 中间人检测"),
        ("ports", "端口连通性"),
        ("throughput", "真实吞吐量"),
        ("note", "备注"),
    ]
    for key, title in order:
        if key not in payload:
            continue
        val = payload[key]
        log("[%s]" % title)
        if key == "throughput":
            _show_throughput(val)
        elif isinstance(val, dict):
            for k, v in val.items():
                log("    %-22s %s" % (k, v))
        else:
            log("    %s" % val)

    extra = {k: v for k, v in payload.items() if k not in dict(order)}
    if extra:
        log("[其他]")
        for k, v in extra.items():
            log("    %-22s %s" % (k, v))

    log("-" * 70)
    log("结论速览：")
    ports = payload.get("ports") or {}
    # 注意字段名要和 agent 写的一致："明文" / "TLS"
    ok = [str(p) for p, r in ports.items()
          if isinstance(r, dict) and (r.get("明文") or r.get("TLS"))]
    if ok:
        log("    ✅ 可用端口：%s" % ", ".join(sorted(ok, key=lambda x: int(x))))
        log("       -> 把 config.toml 里的 relay_ports 首位改成其中之一")
    else:
        log("    ❌ 没有任何端口连通 —— 公司网络可能禁止直连外部 IP，需要换方案")

    tp = payload.get("throughput") or {}
    up = tp.get("up") or []
    if up:
        best = max(float(v) for v in up)
        if best > 2000:
            log("    ⚠️  上行 %.0f Mbps 不合常理（疑似 loopback/局域网），需对真实 IP 重测" % best)
        elif best < 3.0:
            log("    ⚠️  上行仅 %.2f Mbps —— 低于 VPS 带宽预期，瓶颈在链路而非 VPS" % best)
        else:
            log("    ✅ 上行 %.2f Mbps —— 压缩策略可据此放宽" % best)

    egress = payload.get("egress") or {}
    if egress.get("mitm_suspected") is True:
        log("    ⚠️  疑似存在 TLS 中间人解密 —— config.toml 的 pinned_fingerprint 务必留空")
    elif egress.get("mitm_suspected") is False:
        log("    ✅ 未发现 TLS 中间人")

    log("=" * 70)


def _show_throughput(tp):
    """格式化打印吞吐量结果。"""
    if not isinstance(tp, dict) or not tp:
        log("    (未测试)")
        return
    log("    通道: %s   端口: %s" % (tp.get("channel", "?"), tp.get("port", "?")))
    for key, label in (("up", "上行(公司->VPS)"), ("down", "下行(VPS->公司)")):
        vals = tp.get(key) or []
        if not vals:
            log("    %-16s 未测到" % label)
            continue
        nums = [float(v) for v in vals]
        log("    %-16s 各次: %s" % (label, ", ".join("%.2f" % n for n in nums)))
        log("    %-16s 最好 %.2f  最差 %.2f  均值 %.2f  Mbps"
            % ("", max(nums), min(nums), sum(nums) / len(nums)))
    for err in (tp.get("errors") or []):
        log("    ⚠️ %s" % err)
    best = max([float(v) for v in (tp.get("up") or [])] or [0.0])
    if best > 2000:
        log("    ⚠️ 上行 %.0f Mbps 高得不合常理 —— 多半是运行端与目标在同一台机器/"
            "同一局域网（或走了 loopback）。这个数字不代表真实跨网链路能力，"
            "请对着真正的 VPS 公网 IP 重测。" % best)
    elif best > 0:
        for res_name, kb in (("2560x1408 原生", 136.7), ("1920x1056", 82.4), ("1280x704", 34.1)):
            log("    -> %-16s 整帧 %.1fKB，上行可支撑约 %.1f fps 全屏刷新"
                % (res_name, kb, best * 1e6 / 8 / (kb * 1024)))


def _show_throughput(tp):
    """格式化打印吞吐量结果。"""
    if not isinstance(tp, dict) or not tp:
        log("    (未测试)")
        return
    log("    通道: %s   端口: %s" % (tp.get("channel", "?"), tp.get("port", "?")))
    for key, label in (("up", "上行(公司->VPS)"), ("down", "下行(VPS->公司)")):
        vals = tp.get(key) or []
        if not vals:
            log("    %-16s 未测到" % label)
            continue
        nums = [float(v) for v in vals]
        log("    %-16s 各次: %s" % (label, ", ".join("%.2f" % n for n in nums)))
        log("    %-16s 最好 %.2f  最差 %.2f  均值 %.2f  Mbps"
            % ("", max(nums), min(nums), sum(nums) / len(nums)))
    for err in (tp.get("errors") or []):
        log("    ⚠️ %s" % err)
    best = max([float(v) for v in (tp.get("up") or [])] or [0.0])
    if best > 2000:
        log("    ⚠️ 上行 %.0f Mbps 高得不合常理 —— 多半是运行端与目标在同一台机器/"
            "同一局域网（或走了 loopback）。这个数字不代表真实跨网链路能力，"
            "请对着真正的 VPS 公网 IP 重测。" % best)
    elif best > 0:
        log("    -> 对照压缩策略（各分辨率整帧字节数为 Phase 0b 实测）：")
        for res_name, kb in (("2560x1408 原生", 136.7), ("1920x1056", 82.4), ("1280x704", 34.1)):
            log("       %-16s 整帧 %.1fKB，上行可支撑约 %.1f fps 全屏刷新"
                % (res_name, kb, best * 1e6 / 8 / (kb * 1024)))


def read_line(conn, limit=1 << 20):
    """读到换行为止。返回 ``(行, 多读出来的字节)``。

    多读出来的字节**必须保留**：吞吐量测试的载荷紧跟在 JSON 行之后，
    如果这里丢掉，后面的字节计数就会错位。
    """
    data = b""
    while b"\n" not in data and len(data) < limit:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    if b"\n" in data:
        line, _, rest = data.partition(b"\n")
        return line, rest
    return data, b""


def read_exact(conn, n, initial=b"", timeout=120.0):
    """精确读满 n 字节，返回 ``(数据, 耗时秒)``。

    initial 是 read_line 多读出来的部分，已属于本次载荷。
    """
    buf = bytearray(initial[:n])
    conn.settimeout(timeout)
    t0 = time.time()
    while len(buf) < n:
        chunk = conn.recv(min(262144, n - len(buf)))
        if not chunk:
            raise ConnectionError("对端提前断开（收到 %d/%d 字节）" % (len(buf), n))
        buf += chunk
    return bytes(buf), max(1e-6, time.time() - t0)


def handle_throughput(conn, payload, port, mode, peer, leftover):
    """实测真实吞吐量。

    与端口连通性测试不同，这里量的是「公司出口 + 国际链路」这一跳的真实速度 ——
    它通常才是整个方案的真正瓶颈，而不是 VPS 的端口速率。
    """
    direction = str(payload.get("dir", "up"))
    n = int(payload.get("bytes") or 0)
    if n <= 0 or n > 256 * 1024 * 1024:
        conn.sendall((json.dumps({"type": "throughput_result", "error": "非法字节数"}) + "\n").encode())
        return

    if direction == "up":
        try:
            data, secs = read_exact(conn, n, leftover)
        except (ConnectionError, OSError, socket.timeout) as e:
            log("端口 %-5s <- %-21s [%s] 上行吞吐测试中断：%s" % (port, peer, mode, e), "WARN")
            return
        mbps = len(data) * 8 / secs / 1e6
        log("端口 %-5s <- %-21s [%s] 上行 %.2f MB 用时 %.2fs = %.2f Mbps"
            % (port, peer, mode, len(data) / 1048576.0, secs, mbps))
        conn.sendall((json.dumps({
            "type": "throughput_result", "dir": "up",
            "bytes": len(data), "seconds": round(secs, 4), "mbps": round(mbps, 3),
        }) + "\n").encode())
    else:
        # 下行：由服务端发数据，客户端计时接收
        blob = os.urandom(n)
        t0 = time.time()
        try:
            conn.sendall(blob)
        except OSError as e:
            log("端口 %-5s <- %-21s [%s] 下行吞吐测试失败：%s" % (port, peer, mode, e), "WARN")
            return
        secs = max(1e-6, time.time() - t0)
        # 只报告服务端侧耗时作为参考，真实速率由客户端计时读取得出
        log("端口 %-5s <- %-21s [%s] 下行 %.2f MB 已发送（发送耗时 %.2fs，客户端将自行计时）"
            % (port, peer, mode, n / 1048576.0, secs))
        try:
            conn.sendall((json.dumps({
                "type": "throughput_result", "dir": "down", "bytes": n,
                "server_send_seconds": round(secs, 4),
            }) + "\n").encode())
        except OSError:
            pass


def handle(conn, addr, port, tls_ctx):
    peer = "%s:%s" % (addr[0], addr[1])
    mode = "?"
    try:
        conn.settimeout(8)

        # 窥探首字节判断是否为 TLS ClientHello（0x16 = TLS handshake record）
        try:
            first = conn.recv(1, socket.MSG_PEEK)
        except socket.timeout:
            log("端口 %-5s <- %-21s 连接后 8 秒无数据（可能是端口扫描器）" % (port, peer), "WARN")
            return
        except OSError as e:
            log("端口 %-5s <- %-21s 窥探失败：%s" % (port, peer, e), "WARN")
            return

        if not first:
            log("端口 %-5s <- %-21s 连上即断开" % (port, peer), "WARN")
            return

        if first == b"\x16":
            mode = "TLS"
            if tls_ctx is None:
                log("端口 %-5s <- %-21s 尝试 TLS，但服务端未启用 --tls" % (port, peer), "WARN")
                return
            try:
                conn = tls_ctx.wrap_socket(conn, server_side=True)
            except ssl.SSLError as e:
                log("端口 %-5s <- %-21s TLS 握手失败：%s" % (port, peer, e), "WARN")
                return
        else:
            mode = "明文"

        data, leftover = read_line(conn)
        text = data.decode("utf-8", "replace").strip()
        if not text:
            log("端口 %-5s <- %-21s [%s] 无有效数据" % (port, peer, mode), "WARN")
            return

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log("端口 %-5s <- %-21s [%s] 非 JSON：%r" % (port, peer, mode, text[:160]), "WARN")
            return

        # 吞吐量测试是独立通道：它自带字节流，不走普通回执
        if payload.get("type") == "throughput":
            handle_throughput(conn, payload, port, mode, peer, leftover)
            return

        # 回执，让 agent 确认是双向可达
        ack = json.dumps({
            "type": "ack",
            "seen_port": port,
            "mode": mode,
            "server_time": now_str(),
        }, ensure_ascii=False) + "\n"
        try:
            conn.sendall(ack.encode("utf-8"))
        except OSError:
            pass

        if payload.get("type") == "report":
            save_report(payload, peer, port, mode)
            show_report(payload, peer, port, mode)
        else:
            log("端口 %-5s <- %-21s [%s] %s" % (
                port, peer, mode, json.dumps(payload, ensure_ascii=False)[:300]))

    except Exception as e:  # noqa: BLE001 - 侦察工具，单连接异常不应影响服务
        log("端口 %-5s <- %-21s 处理异常：%s: %s" % (port, peer, type(e).__name__, e), "ERROR")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def listen(port, tls_ctx, stop_event):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", port))
    except OSError as e:
        hint = ""
        if port == 22:
            hint = "（22 通常被 SSH 占用 —— 若要让 relay 用 22，需先把 SSH 挪到别的端口）"
        elif port < 1024:
            hint = "（<1024 的端口需要 root，请用 sudo 运行）"
        log("端口 %-5s 绑定失败：%s %s  <- 已跳过" % (port, e, hint), "ERROR")
        srv.close()
        return
    srv.listen(64)
    log("端口 %-5s 监听中" % port)

    while not stop_event.is_set():
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn, addr, port, tls_ctx), daemon=True).start()

    srv.close()


def main():
    global LOG_FILE, REPORT_FILE

    ap = argparse.ArgumentParser(description="ALSPD-DESK Phase 0a 侦察服务端（VPS 侧）")
    ap.add_argument("--ports", default=",".join(str(p) for p in DEFAULT_PORTS),
                    help="要监听的端口，逗号分隔。默认 443,8443,80,8080,22")
    ap.add_argument("--tls", action="store_true",
                    help="启用 TLS（对 TLS ClientHello 自动识别并握手）。强烈建议开启")
    ap.add_argument("--cert", default=None, help="证书路径（.crt）。不填则自动生成自签证书")
    ap.add_argument("--key", default=None, help="私钥路径（.key）。不填则自动生成")
    ap.add_argument("--cert-dir", default=".alspd-probe", help="自签证书存放目录")
    ap.add_argument("--log", default="probe_server.log", help="日志文件。留空字符串可禁用")
    ap.add_argument("--report", default="probe_reports.jsonl", help="侦察报告存档。留空字符串可禁用")
    args = ap.parse_args()

    if args.log:
        LOG_FILE = args.log
    if args.report:
        REPORT_FILE = args.report

    print(BANNER)

    ports = []
    for item in args.ports.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ports.append(int(item))
        except ValueError:
            log("忽略非法端口：%r" % item, "WARN")
    if not ports:
        log("没有有效端口可监听", "ERROR")
        return 1

    tls_ctx = None
    if args.tls:
        try:
            cert, key, fp = (args.cert, args.key, None) if (args.cert and args.key) else ensure_cert(args.cert_dir)
            if fp is None:
                with open(cert, "r", encoding="utf-8") as f:
                    fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()
            tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_ctx.load_cert_chain(cert, key)
            log("TLS 已启用，证书：%s" % cert)
            log("证书 SHA256 指纹：")
            log("    %s" % fp)
            log("    %s" % format_fp(fp))
            log("  -> 传给 agent：--expect-fingerprint %s" % fp)
        except (OSError, ssl.SSLError) as e:
            log("TLS 初始化失败：%s" % e, "ERROR")
            return 1
    else:
        log("未启用 TLS（只测明文）。建议加 --tls 以同时验证 TLS 通道")

    log("将监听端口：%s" % ", ".join(str(p) for p in ports))
    log("日志：%s    报告存档：%s" % (LOG_FILE or "(禁用)", REPORT_FILE or "(禁用)"))
    log("")
    log("现在去公司电脑上运行 probe_agent（或 probe_agent.exe）：")
    log("    python probe_agent.py --host <本机公网IP> --ports %s" % ",".join(str(p) for p in ports))
    log("等待侦察结果中…… 按 Ctrl-C 停止")
    log("")

    stop_event = threading.Event()
    threads = []
    for p in ports:
        t = threading.Thread(target=listen, args=(p, tls_ctx, stop_event), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl-C，正在停止……")
        stop_event.set()
        time.sleep(0.3)

    log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
