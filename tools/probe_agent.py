#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_agent.py —— Phase 0a 侦察工具（公司电脑侧）

用途
----
回答四个决定项目生死的问题：
  1. 未签名的自建 exe 能不能在这台公司电脑上运行（会不会被 EDR 直接干掉）
  2. 能不能连出公司网络
  3. VPS 的哪些端口能连通（443 / 8443 / 80 / 8080 / 22）
  4. TLS 是否被公司中间人解密

特点
----
* 只用 Python 标准库 —— 无需 pip install，可直接 python 运行，也可打包成极小 exe
* 每个端口都同时测「明文」和「TLS」两种通道
* TLS 会记录服务端证书 SHA256 指纹，与 VPS 上 probe_server 打印的指纹比对，
  可确认连到的确实是自己的服务器、而不是中间人
* 结果三处留痕：控制台打印 + 本地文件 + 回报给 VPS（这样即使本地窗口被关掉也有记录）
* 本工具不读取、不保存任何屏幕内容或文件内容

用法
----
    # 源码运行
    python probe_agent.py --host 1.2.3.4

    # exe 双击运行（会提示输入 VPS IP）

    常用参数：
      --host 1.2.3.4                  VPS 公网 IP
      --ports 443,8443,80,8080,22     要测的端口
      --expect-fingerprint <sha256>   VPS 上打印的证书指纹，用于验证没被中间人
      --out probe_result.txt          结果保存路径
"""

import argparse
import ctypes
import hashlib
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import time


def setup_console():
    """Windows 控制台默认 GBK，中文符号会 UnicodeEncodeError。统一切到 UTF-8。

    同时处理两种情况：直接开控制台（改代码页）与输出被重定向到管道（改流编码）。
    """
    if sys.platform == "win32":
        try:
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

# 常见公共 CA 的特征串。leaf 证书的签发者若不含这些，就可能是被中间人替换的证书。
PUBLIC_CA_HINTS = (
    "DigiCert", "Let's Encrypt", "ISRG", "GlobalSign", "Sectigo", "Comodo",
    "GeoTrust", "Thawte", "VeriSign", "Entrust", "Go Daddy", "Godaddy",
    "Amazon", "Google Trust", "Microsoft", "Baltimore", "USERTrust",
    "Certum", "IdenTrust", "SSL.com", "Actalis", "Buypass", "QuoVadis",
    "SwissSign", "Starfield", "SecureTrust", "Trustwave", "CFCA", "WoSign",
    "ZeroSSL", "HARICA", "Telstra", "T-Systems", "D-TRUST", "SecureSite",
    "Certainly", "Viking", "Gandi", "Fastly", "Apple", "OneLogin",
)

DEFAULT_PORTS = [443, 8443, 80, 8080, 22]
EGRESS_TARGETS = ["www.microsoft.com", "www.baidu.com", "www.cloudflare.com"]


# ---------------------------------------------------------------- 小工具

def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def decode_console(raw):
    """Windows 控制台命令输出可能是 GBK，逐个编码试。"""
    for enc in ("utf-8", "gbk", "cp936", "cp437", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def normalize_fp(fp):
    return (fp or "").replace(":", "").replace(" ", "").strip().lower()


def fmt_fp(fp):
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()


def read_line(conn, limit=1 << 20):
    """读到换行为止。返回 ``(行, 多读出来的字节)``。"""
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


def read_exact(conn, n, timeout=120.0):
    """精确读满 n 字节，返回 ``(数据, 耗时秒)``。"""
    conn.settimeout(timeout)
    buf = bytearray()
    t0 = time.perf_counter()
    while len(buf) < n:
        chunk = conn.recv(min(262144, n - len(buf)))
        if not chunk:
            raise ConnectionError("服务端提前断开（收到 %d/%d 字节）" % (len(buf), n))
        buf += chunk
    return bytes(buf), time.perf_counter() - t0


def _open(host, port, use_tls, timeout):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    if use_tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
    return sock


def throughput_once(host, port, use_tls, direction, n_bytes, timeout=180.0):
    """测一次真实吞吐，返回 Mbps。"""
    sock = _open(host, port, use_tls, timeout)
    try:
        sock.sendall((json.dumps({
            "type": "throughput", "dir": direction, "bytes": n_bytes,
        }) + "\n").encode("utf-8"))

        if direction == "up":
            # 用随机字节：真实流量是已压缩的 JPEG，随机数据才有代表性
            blob = os.urandom(n_bytes)
            t0 = time.perf_counter()
            sock.sendall(blob)
            line, _ = read_line(sock)
            wall = time.perf_counter() - t0
            info = json.loads(line.decode("utf-8", "replace"))
            server_secs = float(info.get("seconds") or 0)
            # 分母取「服务端接收耗时」与「本端墙钟耗时」中较大者：
            # 服务端的数字最能反映网络本身，但 loopback/高速局域网下它可能趋近 0，
            # 用墙钟时间兜底可以避免算出虚高的 Mbps。
            secs = max(server_secs, wall, 1e-4)
            return len(blob) * 8 / secs / 1e6
        else:
            _, secs = read_exact(sock, n_bytes, timeout)
            return n_bytes * 8 / secs / 1e6
    finally:
        try:
            sock.close()
        except OSError:
            pass


def measure_throughput(host, port, use_tls, n_bytes, iters, timeout=180.0):
    """双向各测 iters 次，返回 {up:[...], down:[...], errors:[...]}。"""
    out = {"up": [], "down": [], "errors": []}
    for direction, label in (("up", "上行"), ("down", "下行")):
        for i in range(iters):
            try:
                mbps = throughput_once(host, port, use_tls, direction, n_bytes, timeout)
                out[direction].append(mbps)
                print("      %s 第%d次: %.2f Mbps" % (label, i + 1, mbps))
            except Exception as e:
                out["errors"].append("%s第%d次: %s: %s" % (label, i + 1, type(e).__name__, e))
                print("      %s 第%d次失败: %s" % (label, i + 1, e))
                break
    return out


def force_ipv4():
    """某些公司网络 IPv6 走不通但解析会先返回 AAAA，强制只用 IPv4。"""
    try:
        socket.getaddrinfo_orig = socket.getaddrinfo
        def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
            return socket.getaddrinfo_orig(host, port, socket.AF_INET, type, proto, flags)
        socket.getaddrinfo = ipv4_only
    except Exception:
        pass


# ---------------------------------------------------------------- 各项检查

def gather_env():
    env = {}
    env["主机名"] = socket.gethostname()
    env["用户名"] = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    env["计算机网络名"] = os.environ.get("USERDOMAIN", "?")
    env["是否域环境"] = "是" if os.environ.get("USERDNSDOMAIN") else "否"
    try:
        env["Windows 版本"] = "%s (build %s)" % (platform.release(), platform.version())
    except Exception:
        env["Windows 版本"] = platform.platform()
    env["运行方式"] = "PyInstaller exe" if getattr(sys, "frozen", False) else "Python 源码"
    env["程序路径"] = sys.executable
    env["工作目录"] = os.getcwd()
    env["Python"] = sys.version.split()[0]
    try:
        env["管理员权限"] = "是" if ctypes.windll.shell32.IsUserAnAdmin() else "否"
    except Exception as e:
        env["管理员权限"] = "未知 (%s)" % e
    return env


def gather_proxy():
    p = {}
    try:
        import winreg  # 仅 Windows
        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            for field, label in (
                ("ProxyEnable", "IE代理开关"),
                ("ProxyServer", "IE代理地址"),
                ("ProxyOverride", "IE代理例外"),
                ("AutoConfigURL", "PAC脚本地址"),
            ):
                try:
                    p[label] = winreg.QueryValueEx(key, field)[0]
                except FileNotFoundError:
                    p[label] = "(未设置)"
    except Exception as e:
        p["注册表读取"] = "失败: %s" % e

    try:
        out = subprocess.run(["netsh", "winhttp", "show", "proxy"],
                             capture_output=True, timeout=15)
        lines = [l.strip() for l in decode_console(out.stdout).splitlines() if l.strip()]
        p["WinHTTP代理"] = " | ".join(lines) if lines else "(空)"
    except Exception as e:
        p["WinHTTP代理"] = "查询失败: %s" % e
    return p


def check_dns(names):
    res = {}
    for n in names:
        t0 = time.time()
        try:
            ip = socket.gethostbyname(n)
            res[n] = "%s  (%.0f ms)" % (ip, (time.time() - t0) * 1000)
        except Exception as e:
            res[n] = "解析失败: %s" % e
    return res


# ---------------------------------------------------------------- 代理

def proxy_candidates(proxy_info):
    """从已收集的代理信息里整理出可测试的候选代理。

    返回 ``[(scheme, host, port, 来源说明)]``。scheme 取 http 或 socks5。
    """
    out = []
    seen = set()

    def add(scheme, hostport, source):
        hostport = (hostport or "").strip()
        if not hostport or ":" not in hostport:
            return
        host, _, port = hostport.rpartition(":")
        if not host or not port.isdigit():
            return
        # 去掉可能的认证信息 user:pass@host
        host = host.rsplit("@", 1)[-1]
        key = (scheme, host, port)
        if key in seen:
            return
        seen.add(key)
        out.append((scheme, host, int(port), source))

    # 1) 环境变量
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if not value:
            continue
        scheme = "socks5" if value.lower().startswith("socks") else "http"
        add(scheme, value.split("://")[-1], "环境变量 %s" % name)

    # 2) IE 代理设置（注册表）
    server = str(proxy_info.get("IE代理地址") or "")
    enabled = proxy_info.get("IE代理开关")
    if enabled == 1 and server and server != "(未设置)":
        if "=" in server:
            for part in server.split(";"):
                if "=" not in part:
                    continue
                proto, _, value = part.partition("=")
                proto = proto.strip().lower()
                if proto == "socks":
                    add("socks5", value, "IE 代理设置(socks)")
                elif proto in ("http", "https"):
                    add("http", value, "IE 代理设置(%s)" % proto)
        else:
            add("http", server, "IE 代理设置")

    return out


def _proxy_roundtrip(sock, port, tag, timeout):
    """隧道建立后，**必须再做一次真实往返**才算通。

    ⚠️ 只读到 ``200 Connection established`` 是不够的 —— 实测发现
    xray / v2rayN 这类代理对**根本不可达的目标也会先回 200**，
    然后才在背后失败。只看状态行会得到假阳性，
    而这比没有检测更糟：会让人以为代理可用，实际上一连就断。
    """
    try:
        msg = json.dumps({"type": "probe", "mode": "proxy", "port": port, "tag": tag}) + "\n"
        sock.sendall(msg.encode("utf-8"))
        line, _ = read_line(sock)
    except Exception as e:
        return False, "隧道建立后收发失败（目标很可能不可达）：%s" % type(e).__name__
    text = line.decode("utf-8", "replace").strip()
    if not text:
        return False, "隧道建立后无回执 —— 代理先回了 200，实际目标不可达"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False, "回执非 JSON：%r" % text[:60]
    if obj.get("type") != "ack":
        return False, "回执异常：%r" % text[:60]
    return True, "隧道 + 往返均成功"


def test_http_proxy(ph, pp, th, tp, timeout, tag="proxy"):
    """测 HTTP CONNECT 隧道能否**真正**到达目标。返回 (是否成功, 说明)。"""
    try:
        s = socket.create_connection((ph, pp), timeout=timeout)
    except Exception as e:
        return False, "连不上代理 %s:%s (%s)" % (ph, pp, type(e).__name__)
    try:
        s.settimeout(timeout)
        req = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n"
               "Proxy-Connection: Keep-Alive\r\n\r\n" % (th, tp, th, tp))
        s.sendall(req.encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
        if not (line.startswith("HTTP/") and " 200" in line):
            return False, "代理拒绝：%s" % (line or "无响应")
        # 200 只是「代理愿意转发」，还必须确认数据真能穿过去
        return _proxy_roundtrip(s, tp, tag, timeout)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            s.close()
        except OSError:
            pass


def test_socks5_proxy(ph, pp, th, tp, timeout, tag="proxy"):
    """测 SOCKS5 隧道能否**真正**到达目标。返回 (是否成功, 说明)。

    注意：这里**先本地解析目标地址再以 IP 形式发给代理**。
    因为有些 SOCKS 实现（如 python-socks）会在本地解析域名，
    用域名可能先卡在本地 DNS 上，测不到隧道本身。
    """
    try:
        s = socket.create_connection((ph, pp), timeout=timeout)
    except Exception as e:
        return False, "连不上代理 %s:%s (%s)" % (ph, pp, type(e).__name__)
    try:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")                    # 只提一种：无认证
        resp = s.recv(2)
        if len(resp) < 2 or resp[0] != 5:
            return False, "不是 SOCKS5 响应"
        if resp[1] != 0:
            return False, "代理要求认证（方式 0x%02x），本工具不支持带认证的测试" % resp[1]

        try:
            ip = socket.gethostbyname(th)
            packed = socket.inet_aton(ip)
        except OSError as e:
            return False, "本地无法解析目标 %s（SOCKS 需要本机可解析）：%s" % (th, e)

        s.sendall(b"\x05\x01\x00\x01" + packed + int(tp).to_bytes(2, "big"))
        resp = s.recv(10)
        if len(resp) < 2 or resp[0] != 5:
            return False, "响应异常"
        if resp[1] != 0:
            reasons = {1: "一般性失败", 2: "规则不允许", 3: "网络不可达", 4: "主机不可达",
                       5: "连接被拒", 6: "TTL 超时", 7: "命令不支持", 8: "地址类型不支持"}
            return False, "代理返回错误码 0x%02x（%s）" % (resp[1], reasons.get(resp[1], "未知"))
        return _proxy_roundtrip(s, tp, tag, timeout)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            s.close()
        except OSError:
            pass


def probe_proxies(host, direct_ok_port, timeout=8.0):
    """逐个测试候选代理能否到达 VPS。

    「公司网络要求必须经代理出网」是很常见的情况。只测直连的话，
    一旦直连被拦就会得出「这条路走不通」的错误结论 —— 而实际上经代理是通的。
    """
    proxy_info = gather_proxy()
    cands = proxy_candidates(proxy_info)
    if not cands:
        return {"candidates": [], "results": {}, "note": "未探测到任何代理配置"}

    # 优先测 443；如果直连在别的端口通了，也测一下那个端口
    ports = [443]
    if direct_ok_port and int(direct_ok_port) not in ports:
        ports.append(int(direct_ok_port))

    results = {}
    tag = "%s-via-proxy" % socket.gethostname()
    for scheme, ph, pp, source in cands:
        key = "%s://%s:%d" % (scheme, ph, pp)
        results[key] = {"source": source, "per_port": {}}
        for tp in ports:
            if scheme == "socks5":
                ok, detail = test_socks5_proxy(ph, pp, host, tp, timeout, tag)
            else:
                ok, detail = test_http_proxy(ph, pp, host, tp, timeout, tag)
            results[key]["per_port"][str(tp)] = {"ok": ok, "detail": detail}
            print("      %-34s -> %s:%s  %s  %s"
                  % (key, host, tp, "✅ 通" if ok else "❌ 不通",
                     "" if ok else detail[:64]))
    return {"candidates": [list(c) for c in cands], "results": results, "note": ""}


def check_egress_tls(host, port=443, timeout=10):
    """对知名站点做「带校验」的 TLS 握手，用签发者判断是否存在中间人解密。"""
    r = {
        "目标": "%s:%s" % (host, port),
        "TCP可达": False,
        "TLS校验通过": False,
        "证书签发者": None,
        "签发者CN": None,
        "错误": None,
        "mitm_suspected": None,
    }
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            r["TCP可达"] = True
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                subject = dict(x[0] for x in cert.get("subject", ()))
                r["TLS校验通过"] = True
                r["证书签发者"] = issuer.get("organizationName") or issuer.get("commonName")
                r["签发者CN"] = issuer.get("commonName")
                r["证书主体"] = subject.get("commonName")
                blob = "%s %s" % (r["证书签发者"] or "", r["签发者CN"] or "")
                r["mitm_suspected"] = not any(h.lower() in blob.lower() for h in PUBLIC_CA_HINTS)
    except ssl.SSLCertVerificationError as e:
        msg = getattr(e, "verify_message", None) or str(e)
        r["错误"] = "证书校验失败: %s" % msg
        lowered = msg.lower()
        if "self-signed" in lowered or "local issuer" in lowered or "unable to get" in lowered:
            r["mitm_suspected"] = True
        else:
            r["mitm_suspected"] = None
    except Exception as e:
        r["错误"] = "%s: %s" % (type(e).__name__, e)
    return r


def _validate_ack(data: bytes, port: int):
    """校验回执确实来自**我们的** probe_server。

    ⚠️ 只判断「收到任何回复」是不够的 —— 实测踩过这个坑：
    端口 80 上跑着别的 Web 服务，它回了点东西，于是被误判成「明文通」，
    而我们的服务端其实**根本没绑上那个端口**。
    这种假阳性会让人以为端口可用，实际一连就不是我们自己的服务。

    校验方式：回执必须是 ``{"type": "ack", ...}``，且 ``seen_port`` 与所连端口一致。
    注意**不要**拿 ``mode`` 去比对 —— 服务端回的是它自己的中文标签（"明文"/"TLS"），
    客户端发的是 "plain"/"tls"，直接比会把正常回执误判成异常（这个坑也踩过）。
    """
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return False, None, "无回执"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False, text[:200], "回执不是 JSON（该端口上很可能是别的服务）"
    if not isinstance(obj, dict) or obj.get("type") != "ack":
        return False, text[:200], "回执不是本工具的 ack（该端口上很可能是别的服务）"
    seen = obj.get("seen_port")
    if seen is not None:
        try:
            if int(seen) != int(port):
                return False, text[:200], "回执端口不符（期望 %s，收到 %s）" % (port, seen)
        except (TypeError, ValueError):
            return False, text[:200], "回执里的端口字段非法"
    return True, text[:200], None


def probe_plain(host, port, timeout, tag):
    r = {"明文": False, "明文RTTms": None, "明文错误": None, "明文回执": None}
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            msg = json.dumps({"type": "probe", "mode": "plain", "port": port, "tag": tag},
                             ensure_ascii=False) + "\n"
            s.sendall(msg.encode("utf-8"))
            data, _ = read_line(s)
            ok, echo, why = _validate_ack(data, port)
            r["明文"] = ok
            r["明文RTTms"] = round((time.time() - t0) * 1000, 1) if ok else None
            r["明文回执"] = echo
            if not ok:
                r["明文错误"] = why
    except Exception as e:
        r["明文错误"] = "%s: %s" % (type(e).__name__, e)
    return r


def probe_tls(host, port, timeout, tag, expect_fp=None):
    r = {"TLS": False, "TLSRTTms": None, "TLS错误": None, "TLS回执": None,
         "证书指纹": None, "指纹匹配": None}
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                ss.settimeout(timeout)
                der = ss.getpeercert(binary_form=True)
                if der:
                    fp = hashlib.sha256(der).hexdigest()
                    r["证书指纹"] = fp
                    if expect_fp:
                        r["指纹匹配"] = (normalize_fp(fp) == normalize_fp(expect_fp))
                msg = json.dumps({"type": "probe", "mode": "tls", "port": port, "tag": tag},
                                 ensure_ascii=False) + "\n"
                ss.sendall(msg.encode("utf-8"))
                data, _ = read_line(ss)
                ok, echo, why = _validate_ack(data, port)
                r["TLS"] = ok
                r["TLSRTTms"] = round((time.time() - t0) * 1000, 1) if ok else None
                r["TLS回执"] = echo
                if not ok:
                    r["TLS错误"] = why
    except Exception as e:
        r["TLS错误"] = "%s: %s" % (type(e).__name__, e)
    return r


def probe_ports(host, ports, timeout, expect_fp):
    results = {}
    for port in ports:
        print("  正在测试端口 %-5s ..." % port, end="", flush=True)
        tag = "%s-%s" % (socket.gethostname(), port)
        entry = {}
        entry.update(probe_plain(host, port, timeout, tag))
        entry.update(probe_tls(host, port, timeout, tag, expect_fp))

        marks = []
        if entry.get("明文"):
            marks.append("明文OK")
        if entry.get("TLS"):
            marks.append("TLS-OK")
        print(" %s" % ("、".join(marks) if marks else "不通"))

        results[str(port)] = entry
    return results


# ---------------------------------------------------------------- 结果汇总

def build_summary(host, env, proxy, dns, egress, ports, expect_fp, throughput=None,
                  proxy_test=None):
    plain_ok = [p for p, r in ports.items() if r.get("明文")]
    tls_ok = [p for p, r in ports.items() if r.get("TLS")]
    fp_checked = [p for p, r in ports.items() if r.get("指纹匹配") is not None]
    fp_bad = [p for p, r in ports.items() if r.get("指纹匹配") is False]

    lines = []
    lines.append("=" * 66)
    lines.append("  ALSPD-DESK Phase 0a 侦察结果")
    lines.append("  时间: %s    目标: %s" % (now_str(), host))
    lines.append("=" * 66)

    lines.append("")
    lines.append("【1】未签名程序能否运行")
    lines.append("    ✅ 能运行 —— 这个程序本身就跑起来了，说明没被 EDR 直接拦掉")
    lines.append("       (但不代表后续长期驻留 + 外连也不会被拦，仍需观察)")

    lines.append("")
    lines.append("【2】本机环境")
    for k, v in env.items():
        lines.append("    %-16s %s" % (k, v))

    lines.append("")
    lines.append("【3】代理设置")
    for k, v in (proxy or {}).items():
        lines.append("    %-16s %s" % (k, v))

    lines.append("")
    lines.append("【4】DNS 解析")
    for k, v in (dns or {}).items():
        lines.append("    %-22s %s" % (k, v))

    lines.append("")
    lines.append("【5】出网与 TLS 中间人检测")
    for k, v in (egress or {}).items():
        lines.append("    %-16s %s" % (k, v))

    lines.append("")
    lines.append("【6】端口连通性 (VPS %s)" % host)
    for p in sorted(ports, key=lambda x: int(x)):
        r = ports[p]
        tag = []
        if r.get("明文"):
            tag.append("明文通 %.0fms" % (r.get("明文RTTms") or 0))
        elif r.get("明文错误"):
            tag.append("明文不通(%s)" % str(r["明文错误"])[:60])
        if r.get("TLS"):
            tag.append("TLS通 %.0fms" % (r.get("TLSRTTms") or 0))
            if r.get("指纹匹配") is True:
                tag.append("指纹✅匹配")
            elif r.get("指纹匹配") is False:
                tag.append("指纹❌不匹配!")
        elif r.get("TLS错误"):
            tag.append("TLS不通(%s)" % str(r["TLS错误"])[:60])
        lines.append("    端口 %-5s %s" % (p, "  |  ".join(tag)))

    lines.append("")
    lines.append("【7】代理连通性")
    if proxy_test and proxy_test.get("candidates"):
        for key, info in (proxy_test.get("results") or {}).items():
            per_port = info.get("per_port") or {}
            marks = []
            for p, r in sorted(per_port.items(), key=lambda kv: int(kv[0])):
                marks.append("端口%s %s" % (p, "✅通" if r.get("ok") else "❌不通"))
            lines.append("    %-32s %s" % (key, "   ".join(marks)))
            lines.append("    %-32s 来源：%s" % ("", info.get("source", "?")))
            for p, r in per_port.items():
                if not r.get("ok"):
                    lines.append("    %-32s   端口 %s 失败：%s"
                                 % ("", p, str(r.get("detail", ""))[:90]))
    else:
        lines.append("    未探测到代理配置（或已跳过）")

    lines.append("")
    lines.append("【8】真实吞吐量（公司出口 + 国际链路的实际能力）")
    if throughput:
        ch = throughput.get("channel", "?")
        pt = throughput.get("port", "?")
        lines.append("    通道: %s  端口: %s" % (ch, pt))
        for key, label in (("up", "上行 Upload  (公司 -> VPS)"),
                           ("down", "下行 Download(VPS -> 公司)")):
            vals = throughput.get(key) or []
            if vals:
                lines.append("    %-26s 最好 %.2f  Mbps   最差 %.2f Mbps   均值 %.2f Mbps"
                             % (label, max(vals), min(vals), sum(vals) / len(vals)))
            else:
                lines.append("    %-26s 未测到" % label)
        for err in (throughput.get("errors") or []):
            lines.append("    ⚠️ %s" % err)
        best_up = max(throughput.get("up") or [0])
        if best_up > 2000:
            lines.append("")
            lines.append("    ⚠️ 上行 %.0f Mbps 高得不合常理 —— 运行端与目标多半在同一台机器" % best_up)
            lines.append("       或同一局域网（走了 loopback）。这个数字不代表真实跨网链路能力，")
            lines.append("       请对着真正的 VPS 公网 IP 重测。")
        elif best_up > 0:
            lines.append("")
            lines.append("    -> 对照压缩策略（整帧字节数为 Phase 0b 实测的真实屏幕内容 q60）：")
            for res_name, kb in (("2560x1408 原生", 136.7), ("1920x1056", 82.4), ("1280x704", 34.1)):
                fps = best_up * 1e6 / 8 / (kb * 1024)
                lines.append("       %-16s 整帧 %.1fKB -> 上行可支撑约 %.1f fps 全屏刷新"
                             % (res_name, kb, fps))
            lines.append("       （办公场景画面大部分静止，实测中位仅 1~3KB/帧，"
                         "带宽远低于上面的最坏情况）")
    else:
        lines.append("    未测试（跳过了，或没有可用通道）")

    lines.append("")
    lines.append("=" * 66)
    lines.append("  结论")
    lines.append("=" * 66)

    # 代理路径的结论
    proxy_ok_items = []
    for key, info in ((proxy_test or {}).get("results") or {}).items():
        for p, r in (info.get("per_port") or {}).items():
            if r.get("ok"):
                proxy_ok_items.append("%s（端口 %s）" % (key, p))

    if tls_ok or plain_ok:
        best = tls_ok[0] if tls_ok else plain_ok[0]
        how = "TLS" if tls_ok else "明文"
        lines.append("  ✅ 有可用通道")
        lines.append("     优先端口(TLS): %s" % (", ".join(tls_ok) if tls_ok else "无"))
        lines.append("     明文可用端口: %s" % (", ".join(plain_ok) if plain_ok else "无"))
        lines.append("     建议 config.toml: relay_ports = [%s, ...]" % best)
        lines.append("     (%s 通道可用 —— relay 会走 TLS，安全)" % how)
        lines.append("     直连可用，无需代理（config 里 proxy 保持 \"auto\" 即可）")
    else:
        lines.append("  ❌ 直连没有任何端口连通")
        lines.append("     公司网络可能禁止直连外部 IP，或 VPS 侧没在监听。")
        lines.append("     请先确认 probe_server.py 是否已在 VPS 上运行。")
        if proxy_ok_items:
            lines.append("")
            lines.append("  ✅ 但是**经代理可以连上**！这很关键 —— 方案仍然可行。")
            for item in proxy_ok_items:
                lines.append("       %s" % item)
            lines.append("")
            lines.append("     请在两端（Agent 与 Viewer）的 config.toml 里设置：")
            lines.append("       [common]")
            lines.append("       proxy = \"%s\"" % proxy_ok_items[0].split("（")[0])
            lines.append("     （默认的 \"auto\" 理论上会自动兜住，但显式指定更稳妥）")
        else:
            lines.append("     代理也都不可用 —— 这条路可能真的走不通，需要换方案。")

    if fp_bad:
        lines.append("")
        lines.append("  🔴 危险：端口 %s 的证书指纹与预期不符！" % ", ".join(fp_bad))
        lines.append("     说明中途有设备替换了证书（TLS 中间人），或连错了服务器。")
    elif fp_checked:
        lines.append("")
        lines.append("  ✅ TLS 证书指纹与预期一致，连到的确实是自己的服务器")

    mitm = (egress or {}).get("mitm_suspected")
    if mitm is True:
        lines.append("")
        lines.append("  ⚠️  疑似存在 TLS 中间人解密")
        lines.append("     -> config.toml 的 pinned_fingerprint 请务必留空")
        lines.append("     -> 我们的应用层端到端加密仍能保证安全，中间人只看到密文")
    elif mitm is False:
        lines.append("")
        lines.append("  ✅ 未发现 TLS 中间人")

    lines.append("")
    lines.append("=" * 66)
    return "\n".join(lines), {"plain_ok": plain_ok, "tls_ok": tls_ok, "fp_bad": fp_bad}


# ---------------------------------------------------------------- 主流程

def load_host_from_config():
    """支持把参数写进同目录的 probe_config.json，方便打包成 exe 后直接用。"""
    for name in ("probe_config.json",):
        path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                print("已从 %s 读取配置" % path)
                return cfg
            except Exception as e:
                print("读取 %s 失败: %s" % (path, e))
    return {}


def main():
    cfg = load_host_from_config()
    ap = argparse.ArgumentParser(description="ALSPD-DESK Phase 0a 侦察客户端（公司电脑侧）")
    ap.add_argument("--host", default=cfg.get("host"), help="VPS 公网 IP 或域名")
    ap.add_argument("--ports", default=",".join(str(p) for p in cfg.get("ports", DEFAULT_PORTS)),
                    help="要测试的端口，逗号分隔")
    ap.add_argument("--expect-fingerprint", default=cfg.get("expect_fingerprint", ""),
                    help="VPS 上 probe_server 打印的证书 SHA256 指纹，用于确认没被中间人")
    ap.add_argument("--timeout", type=float, default=6.0, help="单次连接超时秒数")
    ap.add_argument("--out", default=None, help="结果保存路径")
    ap.add_argument("--no-report", action="store_true", help="不回传报告给 VPS，只在本地显示")
    ap.add_argument("--no-throughput", action="store_true",
                    help="跳过真实吞吐量实测（默认会测；它会消耗约 8MB 流量）")
    ap.add_argument("--bytes", type=int, default=2 * 1024 * 1024,
                    help="吞吐测试每次的字节数，默认 2MB")
    ap.add_argument("--iters", type=int, default=2,
                    help="每个方向测几次，默认 2（取最好的一次更能反映链路真实能力）")
    args = ap.parse_args()

    print()
    print("=" * 66)
    print("  ALSPD-DESK  Phase 0a  侦察客户端（公司电脑侧）")
    print("=" * 66)
    print("  本工具只测试网络连通性，不读取、不保存任何屏幕或文件内容。")
    print()

    host = args.host
    if not host:
        try:
            host = input("  请输入 VPS 公网 IP：").strip()
        except EOFError:
            host = ""
    if not host:
        print("  ❌ 没有指定 VPS 地址，无法继续。")
        _pause_if_frozen()
        return 2

    ports = []
    for item in str(args.ports).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ports.append(int(item))
        except ValueError:
            print("  忽略非法端口: %r" % item)
    if not ports:
        print("  ❌ 没有有效端口可测。")
        _pause_if_frozen()
        return 2

    expect_fp = args.expect_fingerprint or ""
    if expect_fp:
        print("  证书指纹校验: 已启用")
    else:
        print("  证书指纹校验: 未启用（建议从 VPS 上复制指纹后加上 --expect-fingerprint）")
    print()

    force_ipv4()

    print("[1/7] 收集本机环境 ...")
    env = gather_env()
    print("      %s / %s / 管理员=%s" % (env.get("主机名"), env.get("Windows 版本"),
                                        env.get("管理员权限")))
    print("      ✅ 程序能运行 —— 未被 EDR 直接拦截")

    print("[2/7] 读取代理设置 ...")
    proxy = gather_proxy()

    print("[3/7] 测试 DNS 解析 ...")
    dns = check_dns(["www.baidu.com", "www.microsoft.com", host])

    print("[4/7] 测试出网与 TLS 中间人 ...")
    egress = None
    for target in EGRESS_TARGETS:
        res = check_egress_tls(target, 443, timeout=args.timeout)
        if res.get("TCP可达"):
            egress = res
            state = "OK" if res.get("TLS校验通过") else "校验失败"
            print("      %s -> %s" % (target, state))
            break
    if egress is None:
        egress = {"目标": "全部失败", "错误": "无法连接任何知名 HTTPS 站点", "mitm_suspected": None}
        print("      ❌ 无法连接任何知名 HTTPS 站点")

    print("[5/7] 测试 VPS 端口连通性 ...")
    ports_result = probe_ports(host, ports, args.timeout, expect_fp)

    tls_ok = [p for p, r in ports_result.items() if r.get("TLS")]
    plain_ok = [p for p, r in ports_result.items() if r.get("明文")]

    # ---- 代理连通性 ----
    # 「公司网络要求必须经代理出网」很常见。只测直连的话，一旦直连被拦就会
    # 得出「这条路走不通」的错误结论 —— 而实际上经代理是通的。
    print("[6/7] 测试代理能否到达 VPS ...")
    direct_ok_port = (tls_ok or plain_ok or [None])[0]
    if not proxy_candidates(gather_proxy()):
        proxy_result = {"candidates": [], "results": {}, "note": "未探测到任何代理配置"}
        print("      未探测到代理配置，跳过")
    else:
        proxy_result = probe_proxies(host, direct_ok_port, timeout=args.timeout)

    # ---- 真实吞吐量实测 ----
    # 量的是「公司出口 + 国际链路」这一跳的真实速度。它通常才是整个方案的瓶颈，
    # 而不是 VPS 的端口速率 —— 供应商标的 100Mbps 不代表公司能跑满。
    throughput = None
    if args.no_throughput:
        print("[7/7] 吞吐量实测: 已跳过（--no-throughput）")
    elif not (tls_ok or plain_ok):
        print("[7/7] 吞吐量实测: 跳过（没有可用通道）")
    else:
        use_tls = bool(tls_ok)
        tp_port = int(tls_ok[0] if tls_ok else plain_ok[0])
        total_mb = args.bytes * args.iters * 2 / 1048576.0
        print("[7/7] 实测真实带宽（%s 通道 / 端口 %s，共约 %.1f MB 流量）..."
              % ("TLS" if use_tls else "明文", tp_port, total_mb))
        throughput = measure_throughput(host, tp_port, use_tls, args.bytes, args.iters)
        throughput["channel"] = "TLS" if use_tls else "明文"
        throughput["port"] = tp_port

    payload = {
        "type": "report",
        "sent_at": now_str(),
        "host": host,
        "env": env,
        "proxy": proxy,
        "dns": dns,
        "egress": egress,
        "ports": ports_result,
        "proxy_test": proxy_result,
        "throughput": throughput,
    }
    summary, verdict = build_summary(host, env, proxy, dns, egress, ports_result,
                                     expect_fp, throughput, proxy_result)

    out_path = args.out or os.path.join(
        os.path.dirname(os.path.abspath(sys.argv[0])),
        "probe_result_%s.txt" % socket.gethostname(),
    )
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(summary + "\n")
        print("\n  结果已保存: %s" % out_path)
    except OSError as e:
        print("\n  ⚠️ 结果保存失败: %s" % e)

    print()
    print(summary)

    # 回传给 VPS —— 这样即使本地窗口被关掉，VPS 上也有完整记录
    if not args.no_report:
        sent = False
        for prefer_tls in (True, False):
            for p in ports:
                r = ports_result.get(str(p), {})
                key = "TLS" if prefer_tls else "明文"
                if r.get(key):
                    if _send_report(host, p, prefer_tls, payload, args.timeout):
                        print("  ✅ 报告已回传到 VPS 端口 %s (%s)，可在 VPS 上查看" % (p, key))
                        sent = True
                        break
            if sent:
                break
        if not sent:
            print("  ⚠️ 报告未能回传（没有可用通道），请把上面的结果手动抄给 VPS 侧")

    _pause_if_frozen()
    return 0


def _send_report(host, port, use_tls, payload, timeout):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock.settimeout(timeout)
            if use_tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=host)
                sock.settimeout(timeout)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            read_line(sock)
            return True
        finally:
            try:
                sock.close()
            except OSError:
                pass
    except Exception:
        return False


def _pause_if_frozen():
    if getattr(sys, "frozen", False):
        try:
            input("\n按回车键退出 ...")
        except EOFError:
            pass


if __name__ == "__main__":
    sys.exit(main())
