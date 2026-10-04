#!/usr/bin/env python3
"""Windows 微信/企业微信 客户端控件探测工具。

用途：在决定走 PC Hook 路线之前，先搞清楚当前客户端到底暴露了哪些控件、
消息列表能不能区分发言人和方向。探测不出来，就不要写自动发送。

    pip install pywinauto
    python bridge/inspect_wechat.py                 # 列出所有顶层窗口
    python bridge/inspect_wechat.py --handle 12345  # 打印该窗口的控件树
    python bridge/inspect_wechat.py --find 微信      # 按标题模糊找窗口

只读，不会点击、不会发送任何消息。
"""

from __future__ import annotations

import argparse
import sys

try:
    from pywinauto import Desktop
    from pywinauto.application import Application
except ImportError:
    print("需要先安装：pip install pywinauto（仅 Windows 可用）", file=sys.stderr)
    raise SystemExit(2)


def list_windows(keyword: str = "") -> None:
    print(f"{'handle':>10}  title")
    print("-" * 70)
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text()
        except Exception:
            continue
        if not title:
            continue
        if keyword and keyword not in title:
            continue
        print(f"{w.handle:>10}  {title!r}")
    print("\n找到目标后：python bridge/inspect_wechat.py --handle <handle>")


def dump_tree(handle: int) -> None:
    app = Application(backend="uia").connect(handle=handle)
    win = app.window(handle=handle)
    win.print_control_identifiers(depth=None)


def checklist() -> None:
    print("""
拿到控件树后，逐条确认下面 6 项。任何一项拿不到，PC Hook 方案就没验证通过：

  1. 会话列表控件，以及每个会话项的可稳定定位属性
  2. 当前聊天窗口标题（用来做发送前 verify_target）
  3. 消息列表控件（ScrollPattern / ListItem 集合）
  4. 每条消息能否区分：发言人、正文、方向（自己/对方）
  5. 输入框控件
  6. 发送动作（按钮点击 or Enter 键）

特别提醒：
  - 控件的 runtime id 不能当永久消息 ID 用，它每次渲染都会变。
    必须自己维护"已处理到哪一条"的持久化位置。
  - 同名群/联系人不能用搜索的第一个结果，必须先确认再发。
  - 只比对文本无法区分自己和对方，也处理不了重复内容（两次"催一下"）。
  - 锁屏、远程桌面断开、客户端最小化都会让 UI 自动化失效，需要实测确认。
""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", type=int, help="要打印控件树的窗口 handle")
    ap.add_argument("--find", default="", help="按标题关键字过滤顶层窗口")
    ap.add_argument("--checklist", action="store_true", help="打印验证清单")
    args = ap.parse_args()

    if args.handle:
        dump_tree(args.handle)
        checklist()
    elif args.checklist:
        checklist()
    else:
        list_windows(args.find)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
