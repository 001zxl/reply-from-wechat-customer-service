#!/usr/bin/env python3
"""监听诊断工具：实时显示"它看到了什么、判定成什么"，用来做验收。

只读，不发消息（除非显式加 --reply）。

    # 先只观察，什么都不会发出去
    .venv/bin/python bridge/wechat_mac_watch.py --chat "文件传输助手"

    # 同时看模型会怎么回（仍然不发）
    .venv/bin/python bridge/wechat_mac_watch.py --chat "文件传输助手" --think

    # 确认没问题了，才让它真的发
    .venv/bin/python bridge/wechat_mac_watch.py --chat "文件传输助手" --think --reply

验收方法：跑起来之后，用手机给这个会话发一条消息，看下面三件事
  1. 有没有打印「检测到新消息」
  2. 方向是不是 in
  3. --think 时模型给的回复对不对
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.macos_vision import (  # noqa: E402
    MacWeChatVisionChannel, WeChatNotRunning, new_suffix, title_ok,
)
from app import db  # noqa: E402
from app.config import load_knowledge, settings  # noqa: E402
from app.prompts import SYSTEM_PROMPT, build_context_block  # noqa: E402
from app.tools import ToolRegistry  # noqa: E402

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "d": "\033[2m", "0": "\033[0m"}


def ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"{C['d']}[{ts()}]{C['0']} {msg}", flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True, help="要监听的微信会话名（必须完全一致）")
    ap.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒")
    ap.add_argument("--think", action="store_true", help="顺便跑模型，看会怎么回")
    ap.add_argument("--reply", action="store_true", help="真的把回复发出去（默认关闭）")
    ap.add_argument("--rounds", type=int, default=0, help="跑多少轮后退出，0=一直跑")
    ap.add_argument("--debug", action="store_true", help="比对失败时打印两边内容")
    ap.add_argument(
        "--i-have-permission", action="store_true",
        help="确认已获得账号主人的明确同意才允许真实发送。没有这个开关 --reply 会被拒绝。",
    )
    ap.add_argument(
        "--trigger", default="",
        help="开跑后先往该会话发一条消息，用来触发对方回复。"
             "配合会自动回消息的机器人/公众号，就能跑完整的真实闭环。",
    )
    ap.add_argument(
        "--redirect-to", default="",
        help="试运行：把回复改发到另一个会话，而不是发回原会话。"
             "用来在真实流量上验证而不污染真实对话，例：--redirect-to 文件传输助手",
    )
    args = ap.parse_args()

    if args.reply and not args.think:
        print("--reply 需要配合 --think 使用", file=sys.stderr)
        return 2
    if args.reply and not args.i_have_permission:
        print(
            "\n拒绝启动：--reply 会给真实微信发消息。\n"
            "如果你已经和账号主人确认过、并获得明确同意，再加 --i-have-permission。\n"
            "另外目标会话必须写进 .env 的 WECHAT_SEND_ALLOWLIST。\n",
            file=sys.stderr,
        )
        return 2

    db.init_db()
    ch = MacWeChatVisionChannel(watch=[args.chat])

    if not ch.ensure_running(launch=True):
        log(f"{C['r']}微信没有运行，且没能在 25 秒内拉起来。请先打开并登录微信。{C['0']}")
        return 1
    log("微信已就绪")

    log(f"监听会话：{C['g']}{args.chat}{C['0']}")
    log(f"轮询间隔 {args.interval}s | 模型决策 {'开' if args.think else '关'} | "
        f"真实发送 {C['y']+'开'+C['0'] if args.reply else '关（只观察）'}")

    if not title_ok(ch.current_chat_title(), args.chat):
        log("正在打开会话…")
        if not ch.open_conversation(args.chat):
            log(f"{C['r']}打不开会话，请确认名字和微信里完全一致{C['0']}")
            return 1
    log(f"当前会话：{C['g']}{ch.current_chat_title()}{C['0']}")

    llm = None
    if args.think:
        from app.llm import CustomerServiceLLM

        llm = CustomerServiceLLM()
        log(f"模型：{settings.llm.model}")

    ch._last_shot.clear()
    title, baseline = ch.snapshot(args.chat)
    if not title_ok(title, args.chat):
        log(f"{C['r']}当前打开的是 {title!r}，不是 {args.chat!r}{C['0']}")
        return 1
    log(f"基线建立：当前可见 {len(baseline)} 条消息（这些不会被回复）")
    prev = [m.fingerprint for m in baseline]
    ch._last_msgs[args.chat] = prev

    if args.trigger:
        log(f"发送触发消息：{args.trigger!r}")
        res = await ch.send(args.chat, args.trigger)
        log(f"触发消息：{res.status} {res.detail}")
        for _ in range(20):                     # 等对方回消息
            await asyncio.sleep(1.5)
            ch._last_shot.clear()
            _t, again = ch.snapshot(args.chat)
            if title_ok(_t, args.chat) and [x.fingerprint for x in again] != prev:
                prev = [x.fingerprint for x in again]
                break

    print(f"\n{C['y']}监听中…（也可以现在用手机发一条消息）{C['0']}\n", flush=True)

    round_no = 0
    while True:
        round_no += 1
        await asyncio.sleep(args.interval)

        try:
            title, observed = ch.snapshot(args.chat)
        except WeChatNotRunning:
            log(f"{C['y']}微信被关掉了，等待它回来…{C['0']}")
            if not ch.ensure_running(launch=True):
                log(f"{C['r']}微信没回来，退出{C['0']}")
                break
            prev = []
            continue
        except Exception as exc:
            log(f"{C['r']}截图/识别失败：{type(exc).__name__}: {exc}{C['0']}")
            continue

        # 人在用微信时会随时切走会话。这时绝对不能用别的会话的消息当新消息。
        if not title_ok(title, args.chat):
            log(f"{C['y']}会话被切到 {title!r} —— 本轮跳过并重开 {args.chat}{C['0']}")
            ch._last_msgs.pop(args.chat, None)
            prev = []
            ch.open_conversation(args.chat)
            continue

        cur = [m.fingerprint for m in observed]
        fresh = new_suffix(prev, cur)
        if not fresh:
            if cur != prev:
                ch.compare_failures += 1
                log(f"{C['y']}画面变了但比对不上（人工滚动／消息太快）—— 本轮不回复"
                    f"（累计 {ch.compare_failures} 次）{C['0']}")
                if args.debug:
                    print(f"    prev({len(prev)}) 末尾: {[x[:28] for x in prev[-2:]]}")
                    print(f"    cur({len(cur)}) 开头: {[x[:28] for x in cur[:2]]}")
            prev = cur
            if args.rounds and round_no >= args.rounds:
                break
            continue

        prev = cur
        log(f"{C['g']}检测到新消息 {len(fresh)} 条{C['0']}")
        by_print = {m.fingerprint: m for m in observed}
        for fp in fresh:
            side, _, text = fp.partition("|")
            m = by_print.get(fp)
            color = C["g"] if side == "in" else C["y"]
            tag = "对方" if side == "in" else "自己"
            sender = f" ({m.sender})" if m and m.sender else ""
            print(f"    {color}[{tag}]{C['0']}{sender} {text[:120]}")

            if side != "in" or not args.think:
                continue

            history = db.recent_history(m.conversation_id if m else args.chat, limit=20)
            ctx = build_context_block(
                title=args.chat, is_group=bool(m and m.sender), merchant="",
                knowledge=load_knowledge(), open_cases=[],
            )
            outcome = await llm.decide(
                conversation_id=args.chat, batch_id=f"watch-{round_no}",
                history=history, batch_text=text,
                context_block=ctx, system_prompt=SYSTEM_PROMPT,
                tools=ToolRegistry(args.chat),
            )
            d = outcome.decision
            log(f"    模型：action={C['g']}{d.action.value}{C['0']} intent={d.intent.value} "
                f"单号={d.waybill_numbers} 耗时={outcome.latency_ms}ms")
            print(f"    回复：{d.reply}")
            if d.handoff_reason:
                print(f"    {C['y']}需人工：{d.handoff_reason}{C['0']}")

            if args.reply and d.action.value in ("reply", "ask") and d.reply.strip():
                dest = args.redirect_to or args.chat
                if args.redirect_to:
                    body = f"【试运行·本该回给「{args.chat}」】\n{d.reply}"
                else:
                    body = d.reply
                res = await ch.send(dest, body)
                flag = C["g"] if res.status == "sent" else C["r"]
                log(f"    发送：{flag}{res.status}{C['0']} {res.detail}")
                # 改道发送会切走会话，切回来继续监听
                if args.redirect_to:
                    ch.open_conversation(args.chat)
                ch._last_shot.clear()
                _t, again = ch.snapshot(args.chat)
                prev = [x.fingerprint for x in again] if title_ok(_t, args.chat) else []
            elif args.reply:
                log(f"    {C['y']}该动作不自动发送（{d.action.value}），跳过{C['0']}")
        print(flush=True)

        if args.rounds and round_no >= args.rounds:
            break

    log(f"退出（增量比对失败 {ch.compare_failures} 次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
