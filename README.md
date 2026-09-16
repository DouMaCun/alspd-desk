# ALSPD-DESK

**自建 Windows 远程桌面 —— 在家里操作公司电脑，流量只走你自己的服务器。**

不经过向日葵 / TeamViewer / AnyDesk 等任何第三方中转，桌面画面与键鼠操作全程端到端加密，
连你自己的中继服务器也看不到内容。

[English](README.en.md) · 中文

---

## 为什么做这个

远程办公需要连回公司电脑，但把桌面画面交给第三方远控工具，意味着你的屏幕内容、
键鼠操作都要经过别人的服务器。桌面上的东西一旦涉及敏感资料，这个风险不好接受。

ALSPD-DESK 的思路很直接：

- **中继服务器是你自己的**（一台便宜的 VPS 就行），不需要信任任何厂商
- **应用层端到端加密**，中继只转发密文，即使 VPS 被入侵也读不到画面
- 被控端**主动外连**，不开放任何入站端口、不改防火墙、**不需要管理员权限**
- 功能上不追求大而全，只把「能远程办公」这件事做稳

## 特性

| | |
|---|---|
| 🔐 **端到端加密** | AES-256-GCM，会话密钥由双方 nonce 派生；中继没有密钥 |
| 🧩 **两个密钥域分离** | 配对令牌给中继（只能配对），加密密码只给两端（从不发给中继） |
| ⚡ **采集性能** | DXGI 桌面复制（dxcam）优先，失败自动回落 GDI（mss） |
| 📉 **低带宽** | 分块差分 + atlas 单次编码；办公场景实测 **~1 Mbps**，静止时不发包 |
| 🖥️ **多显示器** | 自动还原显示器在虚拟桌面里的偏移，副屏注入不错位 |
| 📋 **剪贴板双向同步** | 纯文本，含回环防护 |
| 🌐 **代理兜底** | 直连优先；不通时自动走 HTTP CONNECT / SOCKS5（含 IE 注册表探测） |
| 🛡️ **四层保命措施** | 急停热键、只读模式、空闲断连、本机活动检测 |
| 🧪 **演练模式** | 只记录「本来会注入什么」，不真的动键鼠 —— 可安全验证注入链路 |
| 📦 **单文件分发** | PyInstaller 打包；控制端一个 exe，被控端支持开机自启 |
| 🔁 **长期稳定** | 反复断开重连实测无线程 / 句柄 / 内存泄漏 |

## 架构

```
 ┌──────────────────┐                                  ┌──────────────────┐
 │  家中 Windows     │                                  │  公司 Windows     │
 │  Viewer          │                                  │  Agent           │
 └────────┬─────────┘                                  └────────┬─────────┘
          │                                                      │
          │   wss://你的VPS:443  (主动外连)                       │   wss://你的VPS:443
          │                                                      │   开机自启 + 断线重连
          │                                                      │
          └──────────► ┌────────────────────┐ ◄────────────────┘
                       │  你的 VPS (中继)     │
                       │  · 同房间两条连接即配对 │
                       │  · 全程只转发密文      │
                       └────────────────────┘
```

两端都**主动外连**中继，所以被控机不需要任何入站端口。
中继只做两件事：按房间号配对、原样转发字节流。

## 安全模型

**关键设计：中继拿不到解密所需的东西。**

| | 谁知道 | 用途 |
|---|---|---|
| `relay_token` | Agent / Viewer / **中继** | 向中继证明身份，**只能用于配对** |
| `password` | Agent / Viewer（**不给中继**） | 派生端到端会话密钥，真正保护内容 |

会话密钥的派生过程：

```
PSK      = Scrypt(password, salt)          # 密码不当密钥直接用
session  = HKDF(PSK, agent_nonce ‖ viewer_nonce)
```

两个 nonce 由双方随机生成、经中继明文交换（nonce 无需保密）；但中继没有 `PSK`，
**无法派生出会话密钥**。所以即使 VPS 被入侵、被查，攻击者拿到的也只是密文。

其他安全措施：

- **抗重放**：每帧带序列号 + GCM nonce 计数器
- **TLS 只是伪装层**：就算公司做了 TLS 中间人解密，解出来的仍是密文，不影响机密性
- **零入站端口**：被控端不监听任何端口，没有可被攻击的入口

## ⚠️ 保命措施

被控端要夺取键鼠，所以代码有 bug 时可能「夺走你自己的键鼠」。
为此实现了四层保护，默认全部开启：

| # | 措施 | 说明 |
|---|---|---|
| 1 | **急停热键** `Ctrl+Alt+Shift+Q` | 按下即暂停注入 + 松开所有按键 + **彻底停止 Agent**（连重连也停掉，需手动重启） |
| 2 | **只读模式** | `allow_input = false` 时**根本不创建注入器**，只上传画面 |
| 3 | **空闲自动断连** | 远程无操作超时后自动断开并松开按键 |
| 4 | **本机活动检测** | 发现本机有人在动鼠标 → 立即暂停注入，把控制权让给本机 |
| + | **断线释放按键** | 任何断线路径都松开全部按键，避免远端留下卡住的键 |
| + | **演练模式** | `input_dry_run = true` 只记录不执行，可安全验证整条注入链路 |

**首次使用请保持 `allow_input = false`**，确认画面链路稳定、并亲自验证过急停热键后再开启注入。

## 快速开始

需要一台有公网 IP 的 VPS（Debian/Ubuntu）和两台 Windows。

### 1. 在 VPS 上部署中继

```bash
# 从开发机上传
scp -r src config.relay.example.toml root@<你的VPS_IP>:/opt/alspd/

# 在 VPS 上
cd /opt/alspd
mv config.relay.example.toml config.relay.toml   # 然后填好 room / relay_token
pip3 install --break-system-packages websockets  # 中继只依赖 websockets

python3 src/relay/server.py --config config.relay.toml
```

中继会在需要时用 `openssl` 自动生成自签证书并打印指纹。

### 2. 在被控端（公司电脑）运行 Agent

```powershell
# 复制 config.example.toml 为 config.toml，填好 common 段与 agent 段

# 先自检（不联网、不注入键鼠）
.\.venv\Scripts\python.exe src\agent\main.py --selftest

# 启动
.\.venv\Scripts\python.exe src\agent\main.py --config config.toml
```

### 3. 在控制端（家里电脑）运行 Viewer

```powershell
.\.venv\Scripts\python.exe src\viewer\main.py --config config.toml

# 确认画面稳定后，想操作键鼠再加：
.\.venv\Scripts\python.exe src\viewer\main.py --config config.toml --enable-input
```

两端配置里的 `relay_host` / `room` / `relay_token` / `password` **必须完全一致**。

> **详细的部署步骤**（VPS systemd 常驻、打包 exe、开机自启、排错表、卸载）
> 见 [`docs/deploy.md`](docs/deploy.md)。

## 配置要点

完整注释见 [`config.example.toml`](config.example.toml)，这里列几个关键项：

```toml
[common]
relay_host = "<你的VPS公网IP>"
relay_ports = [443]          # 443 最优：最易穿公司防火墙、最不显眼
proxy = "auto"               # 直连优先；不通时自动尝试系统探测到的代理

[agent]
allow_input = false          # 【首次务必 false】只读观看
target_width = 0             # 0 = 原生分辨率（文字最清晰）
max_fps = 20
send_queue_max = 8           # 队列满时丢旧帧，宁可掉帧也不累积延迟
```

建议先用 [`tools/probe_agent.py`](tools/probe_agent.py) 在目标网络里实测一遍：
它会告诉你 **能不能跑未签名程序、哪些端口通、TLS 有没有被中间人、哪个代理可用、真实带宽多少**。
详见 [`docs/verify-phase0.md`](docs/verify-phase0.md)。

## 已知限制

- **全走中继，未实现 P2P 直连** —— 延迟和带宽取决于你的 VPS，多一跳。设计上留了位置，但没做。
- **以用户态运行，不控制登录界面 / UAC 提权窗口** —— 这是刻意的取舍：这样就**不需要管理员权限**。
  代价是被控机锁屏后无法远程解锁。若需要，可自行改为以服务方式运行。
- **多块显示器分辨率完全相同时无法区分** —— 此时 Agent 会在自检和启动日志里明确警告，并拒绝猜测
  （注入位置错了比不注入更糟）。
- **剪贴板只支持纯文本**，不含图片 / 文件。
- **未做文件传输**。
- **传输层没有自动证书钉扎** —— 安全性由应用层端到端加密保证，TLS 只负责「看起来像正常 HTTPS」。
  如需钉扎可在配置里填 `tls.pinned_fingerprint`（但公司若做 TLS 中间人解密，需留空）。
- **链路质量取决于你的网络** —— 实测遇到过偶发丢包导致的 0.5~2 秒延迟尖峰（TCP 重传超时）。
  设计上不会累积延迟，但会偶尔卡一下。
- **未做安全审计**。这是一个个人自用工具，不是产品。

## 测试

测试全部可离线复现，**且不会操作你的键鼠或剪贴板**（注入与剪贴板测试全程用
dry-run / 假后端，正是为此）。

```powershell
# 中继 + 配对 + 端到端加密                        期望 18/18
.\.venv\Scripts\python.exe tools\smoke_test_relay.py --host 127.0.0.1 --port 18443 `
    --token <relay_token> --no-tls --wrong-token

# 只读画面链路（确定性，逐块校验）                  期望 12/12
.\.venv\Scripts\python.exe tools\test_e2e_readonly.py
.\.venv\Scripts\python.exe tools\test_e2e_readonly.py --real   # 真实屏幕 + dxcam

# 键鼠注入 + 剪贴板链路（dry-run）                  期望 31/31
.\.venv\Scripts\python.exe tools\test_e2e_input.py

# 注入参数换算与保命措施（dry-run）                  期望 46/46
.\.venv\Scripts\python.exe tools\test_input_safety.py

# 剪贴板同步（假剪贴板后端）                        期望 25/25
.\.venv\Scripts\python.exe tools\test_clipboard.py

# 代理探测与传输（起假代理做真实转发）              期望 33/33
.\.venv\Scripts\python.exe tools\test_proxy.py

# 多显示器坐标还原                                  期望 26/26
.\.venv\Scripts\python.exe tools\test_multimonitor.py

# 长期运行 / 反复重连的资源泄漏                     期望 7/7
.\.venv\Scripts\python.exe tools\test_soak.py

# 真实互联网端到端（需 VPS 上中继已运行）            期望 5/5
.\.venv\Scripts\python.exe tools\verify_real_network.py --config config.toml --seconds 60
```

### 实测数据

被控端 2560×1440 + 境外 VPS + 真实公网链路上的实测：

| 指标 | 结果 |
|---|---|
| 采集后端 | dxcam (DXGI)：有新帧 4.65 ms，画面静止 0.11 ms |
| 回落后端 | mss (GDI)：恒定 33 ms |
| 帧率 | ~20 fps（达到配置上限） |
| 办公场景码率 | **~1.16 Mbps**（2560×1408 原生） |
| 静止时发包 | **0 帧** |
| 基线 RTT | 121 ms（另有偶发 0.5~2s 尖峰） |
| 反复重连 5 轮 | 线程 4→1 完全回收，句柄仅残留 4，内存持平 |

## 项目结构

```
alspd-desk/
├── src/
│   ├── common/     config · crypto · protocol · wssession · proxy · console
│   ├── relay/      server.py          ← 中继（部署在 VPS）
│   ├── agent/      main · capture · encoder · input · clipboard · session
│   └── viewer/     main · session
├── tools/
│   ├── probe_server.py / probe_agent.py   网络侦察（VPS 侧 / 被控端侧，纯标准库）
│   ├── spike_capture.py                   采集性能验证
│   ├── verify_real_network.py             真实互联网端到端验证
│   └── test_*.py / smoke_test_relay.py    各项测试
├── scripts/        build_exe.ps1 · install_autostart.ps1 · remove_autostart.ps1
├── docs/           plan.md · deploy.md · verify-phase0.md · verify-phase2.md
├── config.example.toml
├── config.relay.example.toml
└── requirements.txt
```

## 系统要求

- **被控端 / 控制端**：Windows 10 / 11（x64）
- **中继**：任何能跑 Python 3.10+ 的 Linux，需公网 IP
- **Python**：3.11+（开发环境用的是 3.13）

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/deploy.md`](docs/deploy.md) | 部署指南：VPS systemd、两端配置、日常流程、排错表、卸载 |
| [`docs/plan.md`](docs/plan.md) | 设计文档：架构、协议、加密、**实测数据与踩坑记录** |
| [`docs/verify-phase0.md`](docs/verify-phase0.md) | 网络侦察：动手前先确认这条路能不能走通 |
| [`docs/verify-phase2.md`](docs/verify-phase2.md) | 键鼠注入的手动验证步骤（急停热键等） |

## ⚠️ 免责声明

- 本项目仅供在**你本人拥有、或已获得明确授权**的设备上使用。
- **在公司设备上部署前，请先确认是否符合公司的 IT 与安全政策。** 未经授权在公司设备上
  安装远控工具可能违反规定 —— 这是你的责任，不是本项目的。
- 本项目**未经过安全审计**，按「现状」提供，不附带任何担保。
- 请务必设置足够强的 `password` 与 `relay_token`，并妥善保管配置文件。

## License

[MIT](LICENSE) © 2026 alspd

简单说：你可以自由使用、修改、分发、甚至商用，只要保留版权声明。
软件按「现状」提供，不附带任何担保。

> **注意区分**：MIT 授权的是**软件的使用权**，而上面「免责声明」里关于
> 「只能在你有权访问的设备上使用」「公司设备需先确认 IT 政策」属于**使用须知**，
> 不与 MIT 冲突 —— 授权你的代码，不等于替你的使用场景背书。
