# -*- coding: utf-8 -*-
"""配置读取（TOML）。

三个组件（Agent / Viewer / Relay）共用同一份配置文件格式：

* Agent 与 Viewer 读 ``[common] [tls]`` 以及各自的 ``[agent]`` / ``[viewer]``
* Relay 读 ``[common]`` 中的 ``room`` / ``relay_token``，以及 ``[relay]``

**两个密钥域是分开的，别混淆**（详见 crypto.py 的模块说明）：

===============  ==========================  ========================
配置项            谁知道它                     用途
===============  ==========================  ========================
relay_token      Agent / Viewer / **Relay**   向中继证明身份，只能用于配对
password         Agent / Viewer（**不给中继**） 派生 E2E 会话密钥，真正保护内容
===============  ==========================  ========================

所以即使 VPS 被入侵，攻击者拿到 relay_token 也只能抢占配对，读不到任何画面。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar

DEFAULT_NAMES = ("config.toml", "alspd.toml")

T = TypeVar("T")


class ConfigError(Exception):
    """配置错误。"""


# ---------------------------------------------------------------- 各节定义

@dataclass
class Common:
    relay_host: str = ""
    relay_ports: List[int] = field(default_factory=lambda: [443, 8443, 80, 8080])
    room: str = ""
    relay_token: str = ""
    password: str = ""
    # 代理策略：
    #   "auto"（默认）—— 先直连；所有端口都不通时，再依次尝试系统探测到的代理
    #   "direct"      —— 只直连，完全不用代理
    #   "<uri>"       —— 先直连；不通时用这个代理，例如 "socks5://127.0.0.1:10808"
    #
    # 「先直连」是有意的：直连不经任何第三方服务器，链路最短。代理只作兜底。
    # 公司网络要求必须经代理出网时，auto 会自动兜住。
    proxy: str = "auto"


@dataclass
class Tls:
    enabled: bool = True
    pinned_fingerprint: str = ""


@dataclass
class Agent:
    # 【保命措施】首次测试务必保持 False，只上传画面、不注入键鼠
    allow_input: bool = False
    capture_backend: str = "auto"          # auto | dxcam | mss
    output: Optional[int] = None           # 显示器序号，None = 主屏
    target_width: int = 0                  # 0 = 原生分辨率（文字最清晰）
    tile_size: int = 64
    change_threshold: int = 8
    jpeg_quality_min: int = 40
    jpeg_quality_max: int = 80
    max_fps: int = 20
    keyframe_interval: float = 2.0
    # 【保命措施】远程无操作超时后自动松手
    idle_disconnect_seconds: int = 60
    # 【保命措施】本地急停热键：按下即停止注入并断开
    panic_hotkey: str = "ctrl+alt+shift+q"
    # 【保命措施】本机活动检测：注入前后比对鼠标位置，发现本机有人在动鼠标就让出控制权
    input_activity_guard: bool = True
    # 位置偏差超过这么多像素就认为本机有人在操作鼠标
    input_guard_threshold: int = 12
    # 本机安静这么多秒后自动恢复注入（0 = 不自动恢复，需手动）
    input_guard_auto_resume: float = 3.0
    # 演练模式：只记录「本来会注入什么」，**不真的操作键鼠**。
    # 用来安全地验证整条注入链路是否正常，不必担心失控。
    # 也可用环境变量 ALSPD_INPUT_DRY_RUN=1 临时开启。
    input_dry_run: bool = False

    # 剪贴板同步（纯文本，双向）。远程办公复制粘贴是刚需，默认开启。
    # ⚠️ 剪贴板可能含敏感内容（例如从密码管理器复制的口令）。
    #    两端都是你自己时开启是合理的；不想要就设为 false。
    clipboard_sync: bool = True
    # 轮询间隔（秒）。太小会白耗 CPU，太大会显得迟钝。
    clipboard_interval: float = 0.6
    send_queue_max: int = 8                # 发送队列上限，超过则丢帧（保证低延迟）


@dataclass
class Viewer:
    window_width: int = 1280
    window_height: int = 720
    scale_mode: str = "fit"                # fit | actual | stretch
    # 剪贴板同步（纯文本，双向）
    clipboard_sync: bool = True


@dataclass
class Relay:
    host: str = "0.0.0.0"
    port: int = 443
    cert: str = ""
    key: str = ""


@dataclass
class Logging:
    level: str = "INFO"
    file: str = ""


@dataclass
class AppConfig:
    common: Common = field(default_factory=Common)
    tls: Tls = field(default_factory=Tls)
    agent: Agent = field(default_factory=Agent)
    viewer: Viewer = field(default_factory=Viewer)
    relay: Relay = field(default_factory=Relay)
    logging: Logging = field(default_factory=Logging)
    path: str = ""


# ---------------------------------------------------------------- 加载

def find_config(explicit: Optional[str] = None) -> Optional[Path]:
    """按顺序找配置文件：显式路径 -> 当前目录 -> 程序目录 -> 项目根。"""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None

    here = Path(__file__).resolve()
    candidates: List[Path] = [Path.cwd()]
    candidates.extend(here.parents[:4])          # src/common -> ... -> 项目根
    try:
        import sys
        candidates.append(Path(sys.argv[0]).resolve().parent)
    except Exception:
        pass

    for base in candidates:
        for name in DEFAULT_NAMES:
            p = base / name
            if p.exists():
                return p
    return None


def _build(cls: Type[T], section: Dict[str, Any], where: str) -> T:
    """把 TOML 的一节填进 dataclass，未知键给出提示而不是静默忽略。"""
    known = {f.name: f for f in fields(cls)}
    kwargs: Dict[str, Any] = {}
    unknown: List[str] = []
    for key, value in section.items():
        if key in known:
            kwargs[key] = value
        else:
            unknown.append(key)
    if unknown:
        print("[配置] 警告：%s 节存在已忽略的未知项：%s" % (where, ", ".join(sorted(unknown))))
    return cls(**kwargs)


def load(explicit: Optional[str] = None) -> AppConfig:
    """读取配置。找不到文件时返回全默认值（并提示）。"""
    path = find_config(explicit)
    if path is None:
        print("[配置] 未找到 config.toml，使用内置默认值（大部分功能需要先填配置）")
        return AppConfig()

    try:
        raw_bytes = path.read_bytes()
    except OSError as e:
        raise ConfigError("读取 %s 失败：%s" % (path, e)) from None

    try:
        # 必须用 utf-8-sig：Windows 记事本保存 UTF-8 时会写入 BOM，
        # 而 tomllib 遇到 BOM 会报 “Invalid statement (at line 1, column 1)”，
        # 报错信息完全看不出是 BOM 的问题。这个坑非常容易踩。
        text = raw_bytes.decode("utf-8-sig")
        raw = tomllib.loads(text)
    except UnicodeDecodeError as e:
        raise ConfigError("%s 不是合法的 UTF-8 文本：%s" % (path, e)) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError("%s 格式错误：%s" % (path, e)) from None

    cfg = AppConfig(path=str(path))
    cfg.common = _build(Common, raw.get("common", {}), "common")
    cfg.tls = _build(Tls, raw.get("tls", {}), "tls")
    cfg.agent = _build(Agent, raw.get("agent", {}), "agent")
    cfg.viewer = _build(Viewer, raw.get("viewer", {}), "viewer")
    cfg.relay = _build(Relay, raw.get("relay", {}), "relay")
    cfg.logging = _build(Logging, raw.get("logging", {}), "logging")
    return cfg


# ---------------------------------------------------------------- 校验

def validate_common(cfg: AppConfig, need_password: bool = True) -> List[str]:
    """返回问题列表（空列表 = 通过）。"""
    problems: List[str] = []
    c = cfg.common
    if not c.relay_host:
        problems.append("common.relay_host 未填写（VPS 公网 IP）")
    if not c.room:
        problems.append("common.room 未填写")
    if not c.relay_token:
        problems.append("common.relay_token 未填写（需与中继端一致）")
    if len(c.relay_token) < 16:
        problems.append("common.relay_token 太短，建议 32 位以上随机串")
    if need_password:
        if not c.password:
            problems.append("common.password 未填写（端到端加密密码）")
        elif len(c.password) < 12:
            problems.append("common.password 太短，建议 16 位以上")
    return problems


def normalize_fingerprint(fp: str) -> str:
    """指纹去掉冒号/空格并转小写，便于比对。"""
    return "".join(ch for ch in (fp or "") if ch not in ": \t").lower()
