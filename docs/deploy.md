# 部署指南

三个组件各部署一次：

| 组件 | 部署位置 | 形态 |
|---|---|---|
| **Relay** 中继 | 野草云 Debian VPS | systemd 常驻 |
| **Agent** 受控端 | 公司电脑 | exe + 开机自启 |
| **Viewer** 控制端 | 家里电脑 | 单文件 exe |

> 三个组件的 `relay_host` / `room` / `relay_token` 必须一致；
> `password` **只填 Agent 与 Viewer，绝对不要填到中继上**。

---

## 一、VPS 上部署中继

### 1. 传文件

```powershell
# 在开发机上执行。relay 只依赖标准库 + websockets，所以把整个 src 传过去
scp -r src root@<VPS_IP>:/opt/alspd/
scp requirements.txt root@<VPS_IP>:/opt/alspd/
```

### 2. 装依赖

```bash
ssh root@<VPS_IP>
cd /opt/alspd
apt update && apt install -y python3 python3-pip python3-venv openssl
python3 -m venv .venv
.venv/bin/pip install websockets
```

### 3. 写配置

`/opt/alspd/config.toml`：

```toml
[common]
relay_host = "<VPS_IP>"
relay_ports = [443, 8443, 8080]
room = "你的房间号"
relay_token = "32位以上的随机串"
# ⚠️ 这里不要写 password！中继不需要它，填了反而违背「中继看不到内容」的设计

[tls]
enabled = true

[relay]
host = "0.0.0.0"
port = 443
cert = "/opt/alspd/relay.crt"
key = "/opt/alspd/relay.key"

[logging]
level = "INFO"
file = "/opt/alspd/relay.log"
```

> 证书不填也没关系 —— 首次启动会用 openssl 现场生成自签证书，并打印 SHA256 指纹。

### 4. 先自测一遍

```bash
cd /opt/alspd
.venv/bin/python src/relay/server.py --config config.toml --check
```

### 5. 装成 systemd 服务

`/etc/systemd/system/alspd-relay.service`：

```ini
[Unit]
Description=ALSPD-DESK Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/alspd
ExecStart=/opt/alspd/.venv/bin/python /opt/alspd/src/relay/server.py --config /opt/alspd/config.toml
Restart=always
RestartSec=5
StandardOutput=append:/opt/alspd/relay.log
StandardError=append:/opt/alspd/relay.log

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now alspd-relay
systemctl status alspd-relay
tail -f /opt/alspd/relay.log
```

> **端口冲突**：如果 22 被 SSH 占用，别把 22 放进 `relay_ports`（或先把 SSH 挪走）。
> **证书指纹**：启动日志里会打印 `证书 SHA256`。要开钉扎校验就把它填到两端的
> `tls.pinned_fingerprint`；**但如果公司网络会做 TLS 中间人解密，请留空**，
> 否则连不上（留空也安全 —— 我们另有应用层端到端加密）。

### 6. 加固（可选，但建议）

```bash
apt install -y fail2ban
# 中继本身只接受握过手、验过令牌的连接，且只转发密文，
# 被扫到无害。fail2ban 主要防日志被刷。
```

---

## 二、公司电脑上部署 Agent

### 1. 打包（在开发机上）

```powershell
.\scripts\build_exe.ps1
```

产出：
- `dist\ALSPD-Agent\` 整个目录（`ALSPD-Agent.exe` + 依赖）→ 拷到公司电脑
- `dist\ALSPD-Viewer.exe` 单文件 → 拷回家

> ⚠️ **必须把整个 `ALSPD-Agent` 目录一起拷走**，不能只拷 exe（onedir 形态）。

### 2. 开箱自检

在公司电脑上先跑自检（**不联网、不注入键鼠**）：

```powershell
.\ALSPD-Agent.exe --selftest
```

它会逐项检查：依赖模块 → **真实抓一帧屏幕** → 注入器（演练模式）→ 急停热键 → 配置。
任何一项 ❌ 都会明确告诉你原因。

> 这一步很有必要：打包最容易出"某个模块没进去"，而这类问题只在运行时才暴露。
> 实际开发中就靠它抓到过一次 —— dxcam 的编译扩展没被打进去，
> 导致打包后采集完全失效（报 `No module named 'cv2'`）。

### 3. 放配置

把 `config.example.toml` 复制成 `config.toml`，放在 **exe 同目录**，填写：

```toml
[common]
relay_host = "<VPS_IP>"
relay_ports = [443, 8443, 8080]
room = "你的房间号"
relay_token = "和中继一致的串"
password = "端到端加密密码（中继不知道这个）"

[agent]
allow_input = false      # ← 首次务必 false，只读观看
```

### 4. 先手动跑一次

```powershell
.\ALSPD-Agent.exe
```

看到 `[配对] 已与 viewer 建立端到端加密通道` 就说明通了。

### 5. 开机自启

```powershell
.\scripts\install_autostart.ps1
```

它会在**当前用户的启动文件夹**里放一个快捷方式 —— 不需要管理员权限，不装服务，
不改注册表。脚本还会：

- 提示是否找到 `config.toml`（找不到会警告）
- **如果 `allow_input = true` 会给出醒目警告**（开机后任何拿到密码的人都能操作你键鼠）
- 告诉你急停热键是什么

取消自启：

```powershell
.\scripts\remove_autostart.ps1              # 只取消自启
.\scripts\remove_autostart.ps1 -KillRunning # 顺便结束正在跑的 Agent
.\scripts\remove_autostart.ps1 -Status      # 只看状态
```

---

## 三、家里电脑上运行 Viewer

1. 把 `dist\ALSPD-Viewer.exe` 拷过去
2. 同目录放一份 `config.toml`（`[common]` 与 `[tls]` 部分和 Agent 一致）
3. 先自检：`.\ALSPD-Viewer.exe --selftest`
4. 双击运行

**键鼠注入默认是关闭的**，只读观看。要开启：

```powershell
.\ALSPD-Viewer.exe --enable-input
```

> 开启前请确认 Agent 端 `allow_input = true`，否则 Agent 会忽略注入
> （Viewer 日志里会明确警告这一点）。

---

## 四、日常使用流程

```
早上到公司：什么都不用做（Agent 已开机自启）
晚上在家：  双击 ALSPD-Viewer.exe -> 看到公司电脑画面
            需要操作就加 --enable-input
出事想刹车：按 Ctrl+Alt+Shift+Q（Agent 侧）—— 立即停止注入并彻底关掉 Agent
```

---

## 五、排错

| 现象 | 原因与对策 |
|---|---|
| Agent 报「所有端口都连不上」 | 公司防火墙拦了。先用 `tools/probe_agent.py` 实测 —— 它会同时测**直连**和**经代理**。若直连不通但代理可用，在两端 config 里设 `proxy = "socks5://..."`（详见下一条） |
| 直连被拦，但公司要求必须经代理出网 | 默认 `proxy = "auto"` 会先直连、再自动尝试系统探测到的代理（环境变量 / IE 注册表 / WinHTTP）。想显式指定就填 `proxy = "http://127.0.0.1:7890"` 或 `proxy = "socks5://127.0.0.1:10808"` |
| 代理地址是 PAC 脚本（自动配置脚本） | 本工具**不解析 PAC**。请从 PAC 里找出实际代理地址，手动填到 `proxy`。侦察报告里会提示检测到了 PAC |
| 走 SOCKS 代理时中继地址必须本机可解析 | `python-socks` 会在本地解析主机名。用 VPS 的 **IP** 最省事（本项目默认就是 IP）|
| 一直「等待对端连接」 | 另一端没连上，或 `room` / `relay_token` 不一致 |
| `令牌校验失败` | 中继日志会明确打印。核对三端的 `relay_token` |
| `证书指纹不匹配` | 公司做了 TLS 中间人解密 —— 把两端的 `pinned_fingerprint` 清空即可（仍然安全） |
| Viewer 里画面不动 | 看 Agent 日志有没有 `[状态]` 行；可能对端只读模式、或画面真的没变化（静止时**不发包**是设计如此） |
| 点击有偏移 | 先跑 `ALSPD-Agent.exe --list-monitors` 确认编号。多显示器偏移现在是**自动校正**的；唯一无法区分的情况是多块显示器**分辨率完全相同** —— 此时自检与启动日志会明确警告，建议把 `output` 留空（采集主屏） |
| `SendInput 失败` 在涨 | 目标窗口以管理员权限运行。需要以管理员身份运行 Agent |
| Agent 日志出现 `dxcam 初始化失败：COMError ... 拒绝访问` | **该显示器已被别的程序占用桌面复制器**（Windows 限制每个显示器同时只能有一个）。常见占用者：OBS、录屏/截图工具、其他远程桌面客户端、Xbox Game Bar；会话锁定或显示器关闭也会导致。**不影响使用** —— 会自动回落到 mss/GDI，只是采集慢约 7 倍（33ms vs 4.65ms）。关掉占用程序后重启 Agent 即可恢复 |
| 打包后采集报 `No module named 'cv2'` | 构建时漏了 dxcam 的编译扩展。确认 `scripts/build_exe.ps1` 里有 `--collect-all dxcam` |
| 打包报 `PermissionError: [WinError 5] 拒绝访问 …exe` | 有旧的 `ALSPD-Agent.exe` / `ALSPD-Viewer.exe` 还在运行，占住了文件。打包脚本现已自动结束它们；手动排查用 `Get-Process ALSPD-*` |
| 剪贴板同步没反应 | 看 Agent 自检的 `[5] 剪贴板同步`；`win32clipboard` 缺失时该功能失效（其他不受影响）。Windows 上剪贴板被别的程序占用时会短暂读不到，属正常 |
| 剪贴板内容被互相覆盖成死循环 | 不该发生（两端都有回环防护）。若真出现请反馈 |
| `.ps1` 脚本中文乱码/语法报错 | 脚本被存成了**无 BOM 的 UTF-8**。Windows PowerShell 5.1 按 GBK 读取，必须带 UTF-8 BOM（见 `.editorconfig`） |
| 启动即报配置错误 `Invalid statement (at line 1, column 1)` | 配置文件带 UTF-8 BOM。已兼容（用 `utf-8-sig` 读取），若仍报错请检查文件是否被别的工具改坏 |

---

## 六、卸载

```powershell
# 1) 取消开机自启并结束进程
.\scripts\remove_autostart.ps1 -KillRunning

# 2) 直接删除 ALSPD-Agent 目录即可（它是便携的，不写注册表、不装服务）

# VPS 上：
systemctl disable --now alspd-relay
rm -rf /opt/alspd /etc/systemd/system/alspd-relay.service
systemctl daemon-reload
```
