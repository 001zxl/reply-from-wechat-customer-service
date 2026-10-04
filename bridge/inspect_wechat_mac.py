#!/usr/bin/env python3
"""macOS 微信客户端控件探测。

先用它搞清楚这台机器上的微信（4.x）到底暴露了哪些控件，
再决定消息怎么读、怎么发。只读，不点击、不发送。

    .venv/bin/python bridge/inspect_wechat_mac.py --check
    .venv/bin/python bridge/inspect_wechat_mac.py --tree --depth 6
    .venv/bin/python bridge/inspect_wechat_mac.py --messages
    .venv/bin/python bridge/inspect_wechat_mac.py --find-input
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import ax  # noqa: E402

WECHAT_BUNDLE = "com.tencent.xinWeChat"


def cmd_check() -> int:
    trusted = ax.AS.AXIsProcessTrusted()
    print(f"辅助功能权限 : {'已授权 ✓' if trusted else '未授权 ✗'}")
    if not trusted:
        print("  打开 系统设置 → 隐私与安全性 → 辅助功能，把运行本程序的 App 打勾")
    pid = ax.find_pid(WECHAT_BUNDLE)
    print(f"微信进程 PID : {pid if pid else '未找到（微信没在运行？）'}")
    if not pid:
        return 1
    wins = ax.windows(pid)
    print(f"顶层窗口数   : {len(wins)}")
    for w in wins:
        print(f"  - {ax.describe(w)}")
    return 0 if trusted and pid else 1


def cmd_tree(pid: int, depth: int) -> int:
    ax.ensure_trusted()
    win = ax.main_window(pid)
    if win is None:
        print("找不到微信窗口")
        return 1
    print(ax.dump(win, depth))
    return 0


def cmd_roles(pid: int, depth: int) -> int:
    """统计控件类型，快速判断这棵树有没有可用的东西。"""
    ax.ensure_trusted()
    win = ax.main_window(pid)
    counts: dict[str, int] = {}
    texts: list[str] = []
    for _, node in ax.walk(win, depth):
        r = ax.role(node)
        counts[r] = counts.get(r, 0) + 1
        if r == "AXStaticText":
            t = ax.value(node) or ax.title(node)
            if t:
                texts.append(t)
    print("控件类型统计：")
    for r, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {r}")
    print(f"\nAXStaticText 文本（最多 40 条）：")
    for t in texts[:40]:
        print(f"  {t[:100]!r}")
    return 0


def cmd_messages(pid: int, depth: int) -> int:
    """尽力从消息列表里抽出文本，看看顺序和发言人能不能分辨。"""
    ax.ensure_trusted()
    win = ax.main_window(pid)
    print("窗口标题 :", ax.title(win))
    print("\n--- 所有 AXStaticText（按树的顺序）---")
    n = 0
    for _, node in ax.walk(win, depth):
        if ax.role(node) != "AXStaticText":
            continue
        text = ax.value(node) or ax.title(node)
        if not text:
            continue
        n += 1
        print(f"{n:3d}. {text[:120]!r}")
    print(f"\n共 {n} 条文本")
    return 0


def cmd_find_input(pid: int, depth: int) -> int:
    """找输入框。微信的输入框一般是 AXTextArea。"""
    ax.ensure_trusted()
    win = ax.main_window(pid)
    print("--- 所有输入类控件 ---")
    for _, node in ax.walk(win, depth):
        r = ax.role(node)
        if r in ("AXTextArea", "AXTextField", "AXComboBox"):
            print(f"  {ax.describe(node)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--depth", type=int, default=8)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="检查权限和微信进程")
    g.add_argument("--tree", action="store_true", help="打印控件树")
    g.add_argument("--roles", action="store_true", help="统计控件类型和文本")
    g.add_argument("--messages", action="store_true", help="抽取消息文本")
    g.add_argument("--find-input", action="store_true", help="找输入框")
    args = ap.parse_args()

    if args.check:
        return cmd_check()

    pid = args.pid or ax.find_pid(WECHAT_BUNDLE)
    if not pid:
        print("微信没在运行，先打开微信", file=sys.stderr)
        return 1

    ax.activate(pid)
    if args.tree:
        return cmd_tree(pid, args.depth)
    if args.roles:
        return cmd_roles(pid, args.depth)
    if args.messages:
        return cmd_messages(pid, args.depth)
    if args.find_input:
        return cmd_find_input(pid, args.depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
