#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_clipboard.py —— 剪贴板同步的测试

⚠️ 不会动你真实的剪贴板
-----------------------
测试给 ``ClipboardWatcher`` 注入**假的读写后端**，所以既不会读你剪贴板里的内容，
也不会覆盖它。可以放心运行。

为什么重点测「回环」
--------------------
双向同步最容易出的 bug 就是回环：
A 写入剪贴板 → 监听到变化 → 同步给 B → B 写入 → B 又监听到变化 → 同步回 A → …
两端会无限互相覆盖，还会把链路刷满。

防护办法是「**写入的同时把内容记为已见**」，且这个「写+记」必须和
「读+比对」互斥（同一把锁）。本测试就是围绕这一点展开。

覆盖范围
--------
1. 单条剪贴板文本上限
2. 协议层：ctrl_clipboard / clipboard_text_from 往返 + 超长拒绝
3. 外部变化能被检测到（且只上报一次）
4. **回环防护**：set_text 写入的内容不会被再发回去
5. 相同内容不重复上报
6. 写入失败会被记录而不是静默
7. 过大内容被跳过
8. **并发**：set_text 与监听线程并发时不产生回环
9. 启动时以现有剪贴板为基线（不把已有内容当新变化发出去）

用法
----
    python tools/test_clipboard.py
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from common.console import setup_console         # noqa: E402

setup_console()

from common import protocol                      # noqa: E402
from agent import clipboard as cb                # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  —— " + detail) if detail else ""))
    return ok


class FakeClipboard:
    """模拟系统剪贴板。完全不碰真实剪贴板。"""

    def __init__(self, initial: str = ""):
        self.lock = threading.Lock()
        self.value = initial
        self.writes = 0
        self.fail_writes = False
        self.read_calls = 0

    def read(self):
        with self.lock:
            self.read_calls += 1
            return self.value

    def write(self, text):
        with self.lock:
            if self.fail_writes:
                return False
            self.value = text
            self.writes += 1
            return True

    def external_set(self, text):
        """模拟「本机用户按了 Ctrl+C」—— 由外部改变剪贴板。"""
        with self.lock:
            self.value = text


def wait_for(pred, timeout=4.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


# ============================================================ 测试

def test_limits_and_protocol():
    print("[1] 上限与协议")
    check("单条上限为 1,000,000 字符", cb.MAX_CLIPBOARD_LEN == 1_000_000,
          "%d" % cb.MAX_CLIPBOARD_LEN)
    check("协议与实现的上限一致", protocol.MAX_CLIPBOARD_LEN == cb.MAX_CLIPBOARD_LEN)
    check("正常文本往返", protocol.clipboard_text_from(
        protocol.parse_ctrl(protocol.ctrl_clipboard("你好 hello"))) == "你好 hello")

    try:
        protocol.ctrl_clipboard("x" * (protocol.MAX_CLIPBOARD_LEN + 1))
        check("超长文本被协议层拒绝", False, "居然构造成功了")
    except protocol.ProtocolError:
        check("超长文本被协议层拒绝", True)

    check("空文本解析为空串", protocol.clipboard_text_from({"t": "clipboard", "text": ""}) == "")
    check("非字符串文本解析为空串", protocol.clipboard_text_from({"t": "clipboard", "text": 123}) == "")
    check("缺失文本解析为空串", protocol.clipboard_text_from({"t": "clipboard"}) == "")
    print()


def test_baseline_and_external_change():
    print("[2] 基线与外部变化检测")
    fake = FakeClipboard("启动前就有的内容")
    seen = []
    w = cb.ClipboardWatcher(seen.append, log=lambda m: None, interval=0.1,
                            read_fn=fake.read, write_fn=fake.write)
    w.start()
    try:
        time.sleep(0.4)
        check("启动时不把已有内容当成新变化发出去", len(seen) == 0,
              "上报了 %d 次：%s" % (len(seen), seen[:3]))

        fake.external_set("用户按了 Ctrl+C")
        ok = wait_for(lambda: len(seen) >= 1)
        check("本机外部改变剪贴板 -> 被检测到并上报", ok and seen[-1] == "用户按了 Ctrl+C",
              "seen=%s" % seen[:3])

        time.sleep(0.4)
        check("同一内容不会重复上报", len(seen) == 1, "上报 %d 次" % len(seen))

        fake.external_set("第二次复制")
        wait_for(lambda: len(seen) >= 2)
        check("再次变化能继续上报", len(seen) == 2 and seen[-1] == "第二次复制",
              "seen=%s" % seen)
    finally:
        w.stop()
    print()


def test_loopback_prevention():
    print("[3] 回环防护（最关键的一条）")
    fake = FakeClipboard("初始")
    seen = []
    w = cb.ClipboardWatcher(seen.append, log=lambda m: None, interval=0.1,
                            read_fn=fake.read, write_fn=fake.write)
    w.start()
    try:
        time.sleep(0.3)
        seen.clear()

        # 模拟：对端同步过来一段内容，我们写到本机剪贴板
        ok = w.set_text("来自对端的内容")
        check("set_text 写入成功", ok is True)
        time.sleep(0.6)
        check("【回环防护】写入的内容没有被再发回去", len(seen) == 0,
              "居然上报了 %d 次：%s" % (len(seen), seen[:3]))

        # 之后本机真实变化仍要能上报（防线不能把正常功能也堵掉）
        fake.external_set("本机真实的复制")
        ok = wait_for(lambda: len(seen) >= 1)
        check("回环防护不影响正常的本机变化上报", ok and seen[-1] == "本机真实的复制",
              "seen=%s" % seen)
    finally:
        w.stop()
    print()


def test_many_roundtrips():
    print("[4] 多次往返不产生回环")
    fake = FakeClipboard("初始")
    seen = []
    w = cb.ClipboardWatcher(seen.append, log=lambda m: None, interval=0.08,
                            read_fn=fake.read, write_fn=fake.write)
    w.start()
    try:
        time.sleep(0.2)
        seen.clear()
        for i in range(10):
            w.set_text("对端内容 %d" % i)
            fake.external_set("本机内容 %d" % i)
            time.sleep(0.15)
        time.sleep(0.4)
        check("只上报了本机的 10 次变化（对端写入 10 次均被抑制）",
              len(seen) == 10 and all(s.startswith("本机内容") for s in seen),
              "上报 %d 次：%s" % (len(seen), seen[:4]))
    finally:
        w.stop()
    print()


def test_size_limit_and_failures():
    print("[5] 过大内容与写入失败")
    fake = FakeClipboard("初始")
    seen = []
    logs = []
    w = cb.ClipboardWatcher(seen.append, log=logs.append, interval=0.1,
                            read_fn=fake.read, write_fn=fake.write)
    w.start()
    try:
        time.sleep(0.2)
        seen.clear()

        big = "x" * (cb.MAX_CLIPBOARD_LEN + 1)
        fake.external_set(big)
        time.sleep(0.3)
        check("过大的内容不会被上报", len(seen) == 0, "上报 %d 次" % len(seen))
        check("过大内容计入 skipped_too_long", w.stats["skipped_too_long"] >= 1,
              "%d" % w.stats["skipped_too_long"])
        check("过大内容有明确日志", any("过大" in m for m in logs))

        check("set_text 对过大内容返回 False", w.set_text(big) is False)

        fake.fail_writes = True
        ok = w.set_text("写不进去的内容")
        check("写入失败返回 False 而不是静默", ok is False)
        check("写入失败计入 write_failed", w.stats["write_failed"] >= 1,
              "%d" % w.stats["write_failed"])
        check("写入失败有明确日志", any("失败" in m for m in logs))
    finally:
        w.stop()
    print()


def test_concurrent_no_loopback():
    print("[6] 并发：set_text 与监听线程同时跑也不回环")
    fake = FakeClipboard("初始")
    seen = []
    w = cb.ClipboardWatcher(seen.append, log=lambda m: None, interval=0.01,
                            read_fn=fake.read, write_fn=fake.write)
    w.start()
    try:
        time.sleep(0.2)
        seen.clear()

        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                w.set_text("对端 %d" % i)
                i += 1
                time.sleep(0.005)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(1.2)
        stop.set()
        t.join(timeout=2)
        time.sleep(0.3)

        n_from_remote = sum(1 for s in seen if s.startswith("对端"))
        check("高频写入下完全没有回环（监听线程一次都没上报对端内容）",
              n_from_remote == 0,
              "上报了 %d 次对端内容：%s" % (n_from_remote, seen[:5]))
    finally:
        w.stop()
    print()


def test_read_failure_tolerated():
    print("[7] 读剪贴板失败（被别的程序占用）时不能崩")
    state = {"fail": True}

    def flaky_read():
        if state["fail"]:
            raise RuntimeError("剪贴板被占用")
        return "恢复了"

    seen = []
    errors = []
    w = cb.ClipboardWatcher(seen.append, log=errors.append, interval=0.05,
                            read_fn=flaky_read,
                            write_fn=lambda t: True)
    w.start()
    try:
        time.sleep(0.4)
        check("读取抛异常时线程仍存活",
              w._thread is not None and w._thread.is_alive())
        state["fail"] = False
        ok = wait_for(lambda: len(seen) >= 1)
        check("恢复后能继续工作", ok and seen[-1] == "恢复了", "seen=%s" % seen)
    finally:
        w.stop()
    print()


def main() -> int:
    print()
    print("=" * 74)
    print("  ALSPD-DESK  剪贴板同步测试")
    print("=" * 74)
    print("  ⚠️  全部使用假剪贴板后端：不读、不覆盖你真实的剪贴板内容。")
    print("-" * 74)

    test_limits_and_protocol()
    test_baseline_and_external_change()
    test_loopback_prevention()
    test_many_roundtrips()
    test_size_limit_and_failures()
    test_concurrent_no_loopback()
    test_read_failure_tolerated()

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print()
    print("=" * 74)
    if passed == total:
        print("  ✅ 全部通过：%d/%d" % (passed, total))
        print("  剪贴板同步的解析、上限、回环防护、并发与容错都正确。")
        print("=" * 74)
        return 0
    print("  ❌ 有失败项：%d/%d 通过" % (passed, total))
    for name, ok in results:
        if not ok:
            print("      - %s" % name)
    print("=" * 74)
    return 1


if __name__ == "__main__":
    sys.exit(main())
