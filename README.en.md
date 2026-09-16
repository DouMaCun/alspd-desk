# ALSPD-DESK

**A self-hosted Windows remote desktop — control your office PC from home, with all traffic going only through your own server.**

No third-party relay (Sunlogin, TeamViewer, AnyDesk, …). Screen and input are end-to-end encrypted —
not even your own relay server can see the content.

English · [中文](README.md)

---

## Why this exists

Remote work means connecting back to the office PC. But handing your screen and input to a commercial
remote-desktop vendor means all of it passes through someone else's servers. If your desktop holds
anything sensitive, that's a hard trade to accept.

ALSPD-DESK takes a straightforward position:

- **The relay is yours** — a cheap VPS is enough. No vendor to trust.
- **Application-layer end-to-end encryption** — the relay forwards ciphertext only, so a compromised
  VPS still can't read your screen.
- The controlled machine **dials out**. No inbound ports, no firewall changes, **no admin rights**.
- It doesn't try to do everything. It tries to make "remote office work" solid.

## Features

| | |
|---|---|
| 🔐 **End-to-end encryption** | AES-256-GCM, session key derived from both peers' nonces; the relay holds no key |
| 🧩 **Two separate secrets** | Pairing token lives on the relay (pairing only); the encryption password never leaves the endpoints |
| ⚡ **Fast capture** | DXGI Desktop Duplication (dxcam) preferred, automatic fallback to GDI (mss) |
| 📉 **Low bandwidth** | Tile-based diffing + single-pass atlas JPEG. Measured **~1 Mbps** for office work; **zero packets when idle** |
| 🖥️ **Multi-monitor** | Resolves each display's virtual-desktop offset, so injecting into a secondary screen lands correctly |
| 📋 **Two-way clipboard** | Plain text, with loopback protection |
| 🌐 **Proxy fallback** | Direct first; falls back to HTTP CONNECT / SOCKS5 (including Windows registry proxy detection) |
| 🛡️ **Four safety layers** | Panic hotkey, read-only mode, idle disconnect, local-activity guard |
| 🧪 **Rehearsal mode** | Records what *would* be injected without touching your keyboard/mouse — verifies the whole input path safely |
| 📦 **Single-file distribution** | PyInstaller builds; the viewer is one .exe, the agent supports autostart |
| 🔁 **Long-run stability** | Repeated reconnect cycles: no thread / handle / memory leaks |

## Architecture

```
 ┌──────────────────┐                                  ┌──────────────────┐
 │  Home Windows    │                                  │  Office Windows  │
 │  Viewer          │                                  │  Agent           │
 └────────┬─────────┘                                  └────────┬─────────┘
          │                                                      │
          │   wss://your-vps:443  (outbound)                     │   wss://your-vps:443
          │                                                      │   autostart + auto-reconnect
          │                                                      │
          └──────────► ┌────────────────────┐ ◄────────────────┘
                       │  Your VPS (relay)   │
                       │  · pairs two conns  │
                       │    in the same room │
                       │  · forwards cipher- │
                       │    text only        │
                       └────────────────────┘
```

Both peers **connect outbound**, so the controlled machine needs no inbound port at all.
The relay does exactly two things: pair connections by room, and forward the bytes verbatim.

## Security model

**The core design decision: the relay is never given what it would need to decrypt.**

| | Who holds it | Purpose |
|---|---|---|
| `relay_token` | Agent / Viewer / **relay** | Proves identity to the relay. **Pairing only.** |
| `password` | Agent / Viewer only (**never the relay**) | Derives the end-to-end session key |

Key derivation:

```
PSK      = Scrypt(password, salt)          # the password is never used as a key directly
session  = HKDF(PSK, agent_nonce ‖ viewer_nonce)
```

Nonces are random per session and exchanged in the clear (nonces aren't secret) — but without the
`PSK`, the relay **cannot derive the session key**. So even if the VPS is seized or compromised,
what's recoverable is ciphertext.

Further measures:

- **Replay protection** — every frame carries a sequence number and a GCM nonce counter
- **TLS is camouflage** — even if your employer's network performs TLS interception, what the
  middlebox decrypts is still our ciphertext; confidentiality is unaffected
- **Zero inbound ports** — the agent listens on nothing, so there's no attack surface to reach

## ⚠️ Safety measures

The agent takes over keyboard and mouse. A bug in that code could therefore **take over *yours***.
Four protections are built in and enabled by default:

| # | Measure | Behaviour |
|---|---|---|
| 1 | **Panic hotkey** `Ctrl+Alt+Shift+Q` | Stops injection, releases all keys, and **shuts the agent down entirely** (including reconnects — a manual restart is required) |
| 2 | **Read-only mode** | With `allow_input = false` the injector **is never even created** |
| 3 | **Idle disconnect** | Drops the session and releases keys after a period of inactivity |
| 4 | **Local-activity guard** | If the local user moves the mouse, injection pauses and control goes back to the human |
| + | **Release on disconnect** | Every teardown path releases all keys, so nothing stays stuck remotely |
| + | **Rehearsal mode** | `input_dry_run = true` logs intent without executing — verifies the path safely |

**Keep `allow_input = false` on first use.** Enable injection only after the video path is stable
and you've personally verified the panic hotkey.

## Quick start

You need a VPS with a public IP (Debian/Ubuntu) and two Windows machines.

### 1. Deploy the relay on your VPS

```bash
# From your dev machine
scp -r src config.relay.example.toml root@<YOUR_VPS_IP>:/opt/alspd/

# On the VPS
cd /opt/alspd
mv config.relay.example.toml config.relay.toml   # then set room / relay_token
pip3 install --break-system-packages websockets  # the relay needs only websockets

python3 src/relay/server.py --config config.relay.toml
```

The relay generates a self-signed certificate via `openssl` on first run and prints its fingerprint.

### 2. Run the Agent on the controlled machine

```powershell
# Copy config.example.toml to config.toml and fill in the [common] and [agent] sections

# Self-test first (no network, no input injection)
.\.venv\Scripts\python.exe src\agent\main.py --selftest

# Start
.\.venv\Scripts\python.exe src\agent\main.py --config config.toml
```

### 3. Run the Viewer on the controlling machine

```powershell
.\.venv\Scripts\python.exe src\viewer\main.py --config config.toml

# Once the picture is stable, add input control:
.\.venv\Scripts\python.exe src\viewer\main.py --config config.toml --enable-input
```

`relay_host` / `room` / `relay_token` / `password` **must match on both ends**.

> Full deployment guide (systemd, packaging, autostart, troubleshooting, uninstall):
> [`docs/deploy.md`](docs/deploy.md).

## Configuration highlights

See [`config.example.toml`](config.example.toml) for fully commented options.

```toml
[common]
relay_host = "<YOUR_VPS_IP>"
relay_ports = [443]          # 443 is best: most likely to pass corporate firewalls, least conspicuous
proxy = "auto"               # direct first; falls back to a detected system proxy

[agent]
allow_input = false          # KEEP FALSE on first use — read-only viewing
target_width = 0             # 0 = native resolution (sharpest text)
max_fps = 20
send_queue_max = 8           # drop stale frames rather than accumulate latency
```

Before deploying into a locked-down network, run [`tools/probe_agent.py`](tools/probe_agent.py):
it reports **whether unsigned binaries run, which ports are reachable, whether TLS is intercepted,
which proxy works, and the real bandwidth**. See [`docs/verify-phase0.md`](docs/verify-phase0.md).

## Known limitations

- **Relay-only, no P2P.** Latency and bandwidth depend on your VPS; there's an extra hop. The design
  leaves room for hole punching, but it isn't implemented.
- **Runs in user mode; cannot control the login screen or UAC elevation prompts.** This is a
  deliberate trade: it's what makes **admin rights unnecessary**. The cost is that a locked machine
  can't be unlocked remotely. Run it as a service if you need that.
- **Indistinguishable identical displays.** If multiple monitors share the exact same resolution, the
  agent warns loudly and **refuses to guess** (a wrong injection position is worse than none).
- **Clipboard is plain text only** — no images or files.
- **No file transfer.**
- **No automatic certificate pinning.** Confidentiality rests on the application-layer encryption;
  TLS only makes the traffic look like ordinary HTTPS. You can pin manually via
  `tls.pinned_fingerprint` (but leave it empty if your network intercepts TLS).
- **Link quality is up to your network.** We observed occasional packet-loss spikes causing
  0.5–2 s stalls (TCP retransmission timeouts). The design doesn't accumulate latency, but you will
  see the occasional hiccup.
- **Not security-audited.** This is a personal tool, not a product.

## Testing

All tests run offline and **never touch your keyboard, mouse, or clipboard** — the input and
clipboard suites use dry-run mode and fake backends specifically for that reason.

```powershell
# Relay + pairing + end-to-end encryption            expect 18/18
.\.venv\Scripts\python.exe tools\smoke_test_relay.py --host 127.0.0.1 --port 18443 `
    --token <relay_token> --no-tls --wrong-token

# Read-only video path (deterministic, per-tile)     expect 12/12
.\.venv\Scripts\python.exe tools\test_e2e_readonly.py
.\.venv\Scripts\python.exe tools\test_e2e_readonly.py --real   # real screen + dxcam

# Input + clipboard path (dry-run)                   expect 31/31
.\.venv\Scripts\python.exe tools\test_e2e_input.py

# Injection math and safety measures (dry-run)       expect 46/46
.\.venv\Scripts\python.exe tools\test_input_safety.py

# Clipboard sync (fake clipboard backend)            expect 25/25
.\.venv\Scripts\python.exe tools\test_clipboard.py

# Proxy detection and transport (real fake proxies)  expect 33/33
.\.venv\Scripts\python.exe tools\test_proxy.py

# Multi-monitor coordinate resolution                expect 26/26
.\.venv\Scripts\python.exe tools\test_multimonitor.py

# Long-run / repeated-reconnect resource leaks       expect 7/7
.\.venv\Scripts\python.exe tools\test_soak.py

# Real-internet end-to-end (relay must be running)   expect 5/5
.\.venv\Scripts\python.exe tools\verify_real_network.py --config config.toml --seconds 60
```

### Measured performance

Controlled machine at 2560×1440, overseas VPS, real public-internet path:

| Metric | Result |
|---|---|
| Capture backend | dxcam (DXGI): 4.65 ms per new frame, 0.11 ms when idle |
| Fallback backend | mss (GDI): 33 ms constant |
| Frame rate | ~20 fps (at the configured cap) |
| Office-work bitrate | **~1.16 Mbps** at native 2560×1408 |
| Packets when idle | **zero** |
| Baseline RTT | 121 ms (plus occasional 0.5–2 s spikes) |
| 5 reconnect cycles | threads 4 → 1 fully reclaimed, +4 handles, flat memory |

## Project layout

```
alspd-desk/
├── src/
│   ├── common/     config · crypto · protocol · wssession · proxy · console
│   ├── relay/      server.py          ← relay (deployed on your VPS)
│   ├── agent/      main · capture · encoder · input · clipboard · session
│   └── viewer/     main · session
├── tools/
│   ├── probe_server.py / probe_agent.py   network reconnaissance (stdlib only)
│   ├── spike_capture.py                   capture performance benchmark
│   ├── verify_real_network.py             real-internet end-to-end check
│   └── test_*.py / smoke_test_relay.py    test suites
├── scripts/        build_exe.ps1 · install_autostart.ps1 · remove_autostart.ps1
├── docs/           plan.md · deploy.md · verify-phase0.md · verify-phase2.md
├── config.example.toml
├── config.relay.example.toml
└── requirements.txt
```

## Requirements

- **Agent / Viewer**: Windows 10 / 11 (x64)
- **Relay**: any Linux with Python 3.10+ and a public IP
- **Python**: 3.11+ (developed on 3.13)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Documentation

| File | Contents |
|---|---|
| [`docs/deploy.md`](docs/deploy.md) | Deployment: systemd, both endpoints, daily use, troubleshooting, uninstall |
| [`docs/plan.md`](docs/plan.md) | Design doc: architecture, protocol, encryption, **measurements and lessons learned** |
| [`docs/verify-phase0.md`](docs/verify-phase0.md) | Network reconnaissance — check feasibility before building anything |
| [`docs/verify-phase2.md`](docs/verify-phase2.md) | Manual verification steps for input injection (panic hotkey, etc.) |

## ⚠️ Disclaimer

- Use this only on machines **you own or are explicitly authorised to access.**
- **Check your employer's IT and security policy before deploying this on a company machine.**
  Installing a remote-access tool without authorisation may violate that policy. That responsibility
  is yours, not this project's.
- This project is **not security-audited** and is provided **as-is**, without warranty of any kind.
- Use strong values for `password` and `relay_token`, and keep your config files safe.

## License

[MIT](LICENSE) © 2026 hanxi

In short: use it, modify it, redistribute it, even commercially — just keep the copyright notice.
The software is provided "as is", without warranty of any kind.

> **Note the distinction**: MIT grants you rights to *the software*. The "usage notice" above —
> use it only on machines you're authorised to access, and check your employer's policy first —
> is separate and does not conflict with MIT. Licensing the code isn't an endorsement of
> every way it might be deployed.
