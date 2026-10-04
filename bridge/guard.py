"""微信操作闸 —— 管住两件事：**别乱开会话**、**别乱发消息**。

两种模式（`WECHAT_GUARD` 环境变量）

**whitelist（默认，日常用这个）**
  - 只有登记在 `config/chats.json` 里的会话能被打开和读取，名单外的**直接拒绝**，
    没有"申请一下就能过"这回事。
  - 名单内的会话可以随便读，不用每次批。看消息、做草稿都不需要人管。
  - **发送**是另一道闸：还要 `DRY_RUN=0` 且会话在 `WECHAT_SEND_ALLOWLIST` 里。
  - 默认 `review` 模式，AI 只出草稿，人在 /desk 点一下才发出去。

**strict（敏感场景才用）**
  - 每次操作都要人在终端逐条批（`guard.py review`）。
  - 读一条批一次，用起来很烦，只在临时排查时开。

审计日志：data/wechat_audit.jsonl —— 每次放行和每次拒绝都有记录。

命令行
------
    .venv/bin/python bridge/guard.py list-chats          # 看允许访问哪些会话
    .venv/bin/python bridge/guard.py allow-chat "张三"    # 加一个会话进白名单（一次性）
    .venv/bin/python bridge/guard.py drop-chat "张三"     # 移出白名单
    .venv/bin/python bridge/guard.py audit                # 看历史操作
"""

from __future__ import annotations

import json
import os
import json
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "data" / "wechat_approvals.json"
AUDIT = ROOT / "data" / "wechat_audit.jsonl"

ACTION_LABEL = {
    "launch_wechat": "启动 / 唤出微信",
    "focus_wechat": "把微信窗口调到前台",
    "open_conversation": "打开会话",
    "read_messages": "读取会话消息",
    "snapshot": "截屏读取当前会话",
    "send": "★ 发送消息",
    "scan_chat_list": "扫描微信会话列表（只读名字，用于配置白名单）",
}


_local = threading.local()


def active() -> bool:
    """当前是否已经处在某次已授权操作的范围内（内部子步骤不用重复授权）。"""
    return getattr(_local, "depth", 0) > 0


def wait_seconds() -> float:
    """后台任务用：拿到授权请求后最多挂起等多久。默认 0 = 立刻失败。"""
    try:
        return float(os.environ.get("WECHAT_GUARD_WAIT", "0") or 0)
    except ValueError:
        return 0.0


class ApprovalRequired(RuntimeError):
    """没有有效授权。调用方必须停下来等人批准，不能绕过。"""


def mode() -> str:
    """whitelist（默认）| strict | off"""
    raw = os.environ.get("WECHAT_GUARD", "whitelist").strip().lower()
    if raw in ("off", "0", "false", "no"):
        return "off"
    if raw in ("strict", "on", "1", "true", "yes"):
        # on/1 这些老写法当作 strict，语义更保守
        return "strict"
    return "whitelist"


def enabled() -> bool:
    return mode() != "off"


# ---------------------------------------------------------------- 会话白名单

# 这些动作带"目标会话"，要过白名单
CHAT_SCOPED = {"open_conversation", "read_messages", "snapshot", "send"}

# 这个 target 是"当前打开的那个会话的标题"，不指向特定会话，不算跨会话访问
CURRENT_CHAT = "当前会话标题"

CHATS_CONFIG = ROOT / "config" / "chats.json"


def _norm_name(text: str) -> str:
    return re.sub(r"[\s\u00a0·・\-—_（）()]+", "", text or "").lower()


def allowed_chats() -> list[str]:
    """从 chats.json 读允许访问的会话名。

    只认 macos_wechat 通道的条目 —— 别的通道（企业微信、mock）不走这个闸。
    """
    if not CHATS_CONFIG.exists():
        return []
    try:
        cfg = json.loads(CHATS_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for c in cfg.get("conversations", []):
        if str(c.get("channel", "")).startswith("macos"):
            name = str(c.get("channel_chat_id") or "").strip()
            # 明显是占位符的不算数，免得把"把这里换成XX"当成真会话名
            if name and not any(k in name for k in ("换成", "示例", "<", "（", "REPLACE")):
                out.append(name)
    return out


def chat_allowed(target: str) -> bool:
    """这个会话在不在白名单里。支持前缀匹配（列表里名字会被截断）。"""
    if target == CURRENT_CHAT:
        return True
    want = _norm_name(target)
    if not want:
        return False
    for name in allowed_chats():
        got = _norm_name(name)
        if got == want or (len(got) >= 4 and want.startswith(got)) \
                or (len(want) >= 4 and got.startswith(want)):
            return True
    return False


def add_chat(name: str, title: str = "", mode_: str = "review") -> bool:
    """把一个会话加进白名单（写 chats.json）。这是**一次性**动作，不是每次都要做。"""
    name = (name or "").strip()
    if not name:
        return False
    cfg = {"global": {"default_mode": "off"}, "conversations": []}
    if CHATS_CONFIG.exists():
        try:
            cfg = json.loads(CHATS_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    for c in cfg.setdefault("conversations", []):
        if (str(c.get("channel", "")).startswith("macos")
                and _norm_name(str(c.get("channel_chat_id"))) == _norm_name(name)):
            return False
    cfg["conversations"].append({
        "channel": "macos_wechat",
        "channel_chat_id": name,
        "title": title or name,
        "merchant_id": "",
        "mode": mode_,
        "require_mention_in_group": False,
    })
    CHATS_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def drop_chat(name: str) -> bool:
    if not CHATS_CONFIG.exists():
        return False
    try:
        cfg = json.loads(CHATS_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    before = len(cfg.get("conversations", []))
    cfg["conversations"] = [
        c for c in cfg.get("conversations", [])
        if not (str(c.get("channel", "")).startswith("macos")
                and _norm_name(str(c.get("channel_chat_id"))) == _norm_name(name))
    ]
    if len(cfg["conversations"]) == before:
        return False
    CHATS_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


class ChatNotAllowed(ApprovalRequired):
    """这个会话不在白名单里。不是"等你批"，是直接不许。"""


# ---------------------------------------------------------------- 存取

def _load() -> dict[str, Any]:
    if QUEUE.exists():
        try:
            return json.loads(QUEUE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"requests": []}


def _save(data: dict[str, Any]) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def audit(action: str, target: str, outcome: str, detail: str = "") -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "label": ACTION_LABEL.get(action, action),
            "target": target,
            "outcome": outcome,          # requested | approved | denied | executed | timeout
            "detail": detail[:200],
        }, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 申请与审批

def request(action: str, target: str, note: str = "", ttl: float = 180.0) -> str:
    data = _load()
    req = {
        "id": secrets.token_hex(4),
        "action": action,
        "label": ACTION_LABEL.get(action, action),
        "target": target,
        "note": note,
        "created": time.time(),
        "expires": time.time() + ttl,
        "status": "pending",
        "used": False,
    }
    data.setdefault("requests", []).append(req)
    _save(data)
    audit(action, target, "requested", note)
    return req["id"]


def status(req_id: str) -> str:
    for r in _load().get("requests", []):
        if r["id"] == req_id:
            return str(r["status"])
    return "missing"


def pending() -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for r in _load().get("requests", []):
        if r["status"] == "pending" and now < r["expires"]:
            out.append(r)
    return out


def approve(req_id: str) -> bool:
    data = _load()
    for r in data.get("requests", []):
        if r["id"] == req_id and r["status"] == "pending":
            r["status"] = "approved"
            r["approved_at"] = time.time()
            _save(data)
            audit(r["action"], r["target"], "approved", f"req={req_id}")
            return True
    return False


def deny(req_id: str) -> bool:
    data = _load()
    for r in data.get("requests", []):
        if r["id"] == req_id and r["status"] == "pending":
            r["status"] = "denied"
            _save(data)
            audit(r["action"], r["target"], "denied", f"req={req_id}")
            return True
    return False


def revoke_all() -> int:
    data = _load()
    n = 0
    for r in data.get("requests", []):
        if not r.get("used"):
            r["status"] = "denied"
            n += 1
    _save(data)
    return n


def _take(action: str, target: str) -> bool:
    """找一条已批准、未过期、未被用过、动作和目标都匹配的授权，标记为已用。"""
    data = _load()
    now = time.time()
    for r in data.get("requests", []):
        if (r["status"] == "approved" and not r.get("used")
                and r["action"] == action and r["target"] == target
                and now < r["expires"]):
            r["used"] = True
            r["used_at"] = now
            _save(data)
            return True
    return False


# ---------------------------------------------------------------- 执行闸

@contextmanager
def scope(action: str, target: str, note: str = "",
          wait: float = 0.0) -> Iterator[None]:
    """进入一次微信操作。

    - whitelist 模式：会话类动作查白名单，名单内自由通过，名单外直接拒绝
    - strict 模式：每次都要人一次性批准（保留给敏感场景）
    """
    def _enter():
        _local.depth = getattr(_local, "depth", 0) + 1
        return _local.depth

    def _exit(_d):
        _local.depth = _d - 1

    if not enabled() or active():
        d = _enter()
        try:
            yield
        finally:
            _exit(d)
        return

    # ---------------- 白名单模式（默认）----------------
    if mode() == "whitelist":
        if action in CHAT_SCOPED and not chat_allowed(target):
            allowed = allowed_chats()
            audit(action, target, "denied", "不在会话白名单")
            raise ChatNotAllowed(
                f"「{target}」不在允许访问的会话名单里，已拒绝。\n"
                f"当前名单：{allowed or '（空）'}\n"
                f"要把某个会话加进来（一次即可，不用每次做）：\n"
                f'    .venv/bin/python bridge/guard.py allow-chat "{target}"'
            )
        audit(action, target, "executed", "白名单模式放行")
        d = _enter()
        try:
            yield
        finally:
            _exit(d)
        return

    # ---------------- strict 模式 ----------------
    if _take(action, target):
        audit(action, target, "executed", "复用已有的一次性授权")
        d = _enter()
        try:
            yield
        finally:
            _exit(d)
        return

    wait = wait or wait_seconds()
    req_id = request(action, target, note=note, ttl=max(300.0, wait + 60))
    label = ACTION_LABEL.get(action, action)
    print(f"\n\033[33m[需要授权] {label} → 「{target}」\033[0m", flush=True)
    print("           请在自己的终端里执行："
          ".venv/bin/python bridge/guard.py review", flush=True)

    if wait <= 0:
        audit(action, target, "denied", "无授权且未开启等待")
        raise ApprovalRequired(
            f"没有授权：{label} → 「{target}」。"
            f"请在终端执行 `python bridge/guard.py review` 批准后再试。"
        )

    deadline = time.time() + wait
    while time.time() < deadline:
        st = status(req_id)
        if st == "approved" and _take(action, target):
            audit(action, target, "executed", f"req={req_id}")
            d = _enter()
            try:
                yield
            finally:
                _exit(d)
            return
        if st == "denied":
            audit(action, target, "denied", f"req={req_id}")
            raise ApprovalRequired(f"操作被拒绝：{label} → 「{target}」")
        time.sleep(1.0)

    audit(action, target, "timeout", f"req={req_id}")
    raise ApprovalRequired(f"等待授权超时：{label} → 「{target}」")


# ---------------------------------------------------------------- CLI

def _fmt_age(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def cmd_review(auto: bool = False) -> int:
    """人在自己的终端里逐条批准。程序无法"说服"这个函数做决定 —— 它只是执行输入。

    加 --yes 时会先把清单打出来，然后一次性批准全部待批项。
    """
    print("\n\033[1m微信操作授权 · 逐条审批\033[0m")
    print("每一行代表程序想做的一个动作。批准后只允许执行一次。\n")
    items = pending()
    if not items:
        print("当前没有待批准的操作。\n")
        return 0

    if auto:
        for r in items:
            print(f"  \033[33m[{r['id']}]\033[0m {r['label']} → 「{r['target']}」"
                  + (f"  （{r['note']}）" if r.get("note") else ""))
        print("\n以上是全部待批项。批准后每一条都只能执行一次。")
        try:
            ans = input("全部批准？(y=批准 / 其它=取消) ").strip().lower()
        except EOFError:
            return 0
        if ans in ("y", "yes"):
            for r in items:
                approve(r["id"])
            print(f"\n\033[32m已批准 {len(items)} 条（各仅一次）\033[0m\n")
        else:
            print("已取消，什么都没批。\n")
        return 0
    approved = denied = skipped = 0
    for r in items:
        left = max(0, int(r["expires"] - time.time()))
        print(f"\033[33m[{r['id']}]\033[0m {r['label']} → 「{r['target']}」")
        if r.get("note"):
            print(f"      说明：{r['note']}")
        print(f"      申请于 {_fmt_age(r['created'])}，{left} 秒内有效")
        while True:
            try:
                ans = input("      允许？(y=允许 / n=拒绝 / s=跳过 / q=退出) ").strip().lower()
            except EOFError:
                return 0
            if ans in ("y", "yes"):
                approve(r["id"]); approved += 1
                print("      \033[32m已批准（仅此一次）\033[0m")
                break
            if ans in ("n", "no"):
                deny(r["id"]); denied += 1
                print("      \033[31m已拒绝\033[0m")
                break
            if ans in ("s", "skip", ""):
                skipped += 1
                break
            if ans in ("q", "quit"):
                print(f"\n批准 {approved} / 拒绝 {denied} / 跳过 {skipped}\n")
                return 0
    print(f"\n批准 {approved} / 拒绝 {denied} / 跳过 {skipped}\n")
    return 0


def cmd_list_chats() -> int:
    names = allowed_chats()
    print(f"\n当前模式：\033[1m{mode()}\033[0m")
    if mode() == "whitelist":
        print("白名单内的会话可以自由读取；名单外的直接拒绝。\n")
    else:
        print("strict 模式：每次操作都要你在终端逐条批准。\n")
    if not names:
        print("  （名单是空的 —— 现在任何会话都打不开）")
        print('  加一个：bridge/guard.py allow-chat "会话名"\n')
        return 0
    for n in names:
        print(f"  · {n}")
    print()
    return 0


def cmd_list() -> int:
    items = pending()
    if not items:
        print("没有待批准的操作。")
        return 0
    print(f"\n待批准 {len(items)} 条：")
    for r in items:
        left = max(0, int(r["expires"] - time.time()))
        print(f"  [{r['id']}] {r['label']} → 「{r['target']}」 ({left}s)")
    print()
    return 0


def cmd_audit(limit: int = 40) -> int:
    if not AUDIT.exists():
        print("还没有审计记录。")
        return 0
    lines = AUDIT.read_text(encoding="utf-8").strip().splitlines()[-limit:]
    print("\n最近的操作记录：")
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        mark = {"executed": "✓ 已执行", "denied": "✗ 被拒绝",
                "requested": "… 申请中", "approved": "○ 已批准",
                "timeout": "⏱ 超时"}.get(r["outcome"], r["outcome"])
        print(f"  {r['time']}  {mark:10s} {r['label']} → 「{r['target']}」 {r['detail']}")
    print()
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "review"
    if cmd == "review":
        return cmd_review(auto="--yes" in args or "-y" in args)
    if cmd == "list":
        return cmd_list()
    if cmd == "list-chats":
        return cmd_list_chats()
    if cmd == "allow-chat":
        if len(args) < 2:
            print('用法：guard.py allow-chat "会话名"'); return 1
        ok = add_chat(args[1], title=args[2] if len(args) > 2 else "")
        print(("已加入白名单：" if ok else "已在白名单里，没变：") + args[1])
        return 0
    if cmd == "drop-chat":
        if len(args) < 2:
            print('用法：guard.py drop-chat "会话名"'); return 1
        print(("已移出白名单：" if drop_chat(args[1]) else "名单里没有这个名字：" ) + args[1])
        return 0
    if cmd == "audit":
        return cmd_audit(int(args[1]) if len(args) > 1 else 40)
    if cmd == "revoke":
        print(f"已撤销 {revoke_all()} 条未使用的授权。")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
