#!/usr/bin/env python3
"""多轮真实对话模拟：看它在**一整段**对话里表现如何。

单点用例测的是"这一句答得对不对"，但真实客服是一段连续对话：
连发消息、中途改主意、混着好几件事、夹着情绪、最后还来个结束语。
这个脚本按顺序把整段跑完，每一轮的回复进下一轮的历史，最后回看全局。

    ./run.sh talk                 # 跑全部场景
    ./run.sh talk --scene 1       # 只跑场景一
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PATH", "data/talk_test.db")
os.environ.setdefault("LOGISTICS_PROVIDER", "mock")
os.environ.setdefault("DRY_RUN", "0")

from app import db  # noqa: E402
from app.config import load_knowledge, settings  # noqa: E402
from app.llm import CustomerServiceLLM, looks_like_meta  # noqa: E402
from app.prompts import SYSTEM_PROMPT, build_context_block  # noqa: E402
from app.tools import ToolRegistry  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from run_business import state_present, violates, waybill_present  # noqa: E402

SCENES = json.loads((Path(__file__).parent / "conversations.json").read_text(encoding="utf-8"))

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "d": "\033[2m", "b": "\033[1m", "0": "\033[0m"}


async def run_scene(scene: dict, index: int) -> tuple[int, int, list[str]]:
    print(f"\n{'=' * 78}")
    print(f"{C['b']}场景 {index}：{scene['name']}{C['0']}")
    print(f"{C['d']}{scene['scenario']}{C['0']}")
    print(f"{'=' * 78}")

    llm = CustomerServiceLLM()
    registry = ToolRegistry(f"talk-{index}")
    history: list[dict[str, str]] = list(scene.get("history", []))
    problems: list[str] = []
    checks = 0

    ctx = build_context_block(
        title="电商对接会话", is_group=False, merchant="某电商公司",
        knowledge=load_knowledge(), open_cases=[],
    )

    for turn_no, turn in enumerate(scene["turns"], start=1):
        text = turn["text"]
        outcome = await llm.decide(
            conversation_id=f"talk-{index}", batch_id=f"s{index}t{turn_no}",
            history=history, batch_text=text,
            context_block=ctx, system_prompt=SYSTEM_PROMPT, tools=registry,
        )
        d = outcome.decision
        called = [e.tool for e in outcome.evidence]

        print(f"\n{C['d']}── 第 {turn_no} 轮 {'─' * 55}{C['0']}")
        print(f"  {turn['from']}：{text}")
        print(f"  {C['d']}→ {d.action.value} / {d.intent.value}"
              f"{' / 工具:' + ','.join(called) if called else ''}{C['0']}")
        if d.reply:
            print(f"  助手：{d.reply}")
        else:
            print(f"  助手：{C['d']}（不回复）{C['0']}")
        if d.handoff_reason:
            print(f"  {C['y']}转人工：{d.handoff_reason}{C['0']}")

        # ---- 逐轮断言 ----
        for label, ok in [
            ("动作", not turn.get("expect_action") or d.action.value in turn["expect_action"]),
            ("工具", all(t in called for t in turn.get("must_call_tools", []))),
            ("不提编造", not any(violates(d.reply, b) for b in turn.get("forbid_in_reply", []))),
        ]:
            if turn.get("expect_action") or turn.get("must_call_tools") or turn.get("forbid_in_reply"):
                checks += 1
            if not ok:
                if label == "动作":
                    problems.append(f"第{turn_no}轮 动作应为 {turn['expect_action']}，实际 {d.action.value}")
                elif label == "工具":
                    problems.append(f"第{turn_no}轮 应调用 {turn['must_call_tools']}，实际 {called or '无'}")
                else:
                    bad = [b for b in turn.get("forbid_in_reply", []) if violates(d.reply, b)]
                    problems.append(f"第{turn_no}轮 出现不该有的表述 {bad}")

        checks += 1
        if looks_like_meta(d.reply):
            problems.append(f"第{turn_no}轮 回复是元话语：{d.reply[:60]!r}")
        else:
            print(f"  {C['g']}✓{C['0']} 不是元话语（在回应客户而不是在讲自己的格式）")

        if not outcome.ok:
            problems.append(f"第{turn_no}轮 模型调用失败：{outcome.error}")

        # ---- 把这一轮并进历史（wait 不发消息，不进历史）----
        history.append({"role": "user", "content": text})
        if d.action.value != "wait" and d.reply.strip():
            history.append({"role": "assistant", "content": d.reply})

    # ---- 全局回看 ----
    print(f"\n{C['d']}── 全局回看 {'─' * 55}{C['0']}")
    all_replies = "\n".join(h["content"] for h in history if h["role"] == "assistant")
    gc = scene.get("global_checks", {})

    for wb in gc.get("never_ask_again", []):
        checks += 1
        # 历史里用户只发过一次单号，助手后面不该再要
        asks = [
            h["content"] for h in history
            if h["role"] == "assistant"
            and wb in h["content"] and any(m in h["content"] for m in ("发我", "提供", "给我", "是哪"))
        ]
        if asks:
            problems.append(f"重复索要已给过的单号 {wb}：{asks[0][:50]}")
        else:
            print(f"  {C['g']}✓{C['0']} 没有重复索要单号 {wb}")

    for bad in gc.get("never_claim", []):
        checks += 1
        if violates(all_replies, bad):
            problems.append(f"全程出现了『已经办好了』的说法：「{bad}」")
        else:
            print(f"  {C['g']}✓{C['0']} 全程没有出现「{bad}」")

    if gc.get("why"):
        print(f"  {C['d']}检查目的：{gc['why']}{C['0']}")

    ok = not problems
    print(f"\n  {C['g'] + '✓ 整段对话合格' + C['0'] if ok else C['r'] + '✗ 有问题' + C['0']}")
    for p in problems:
        print(f"    {C['r']}·{C['0']} {p}")
    return (0 if ok else 1), checks, problems


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", type=int, action="append", default=[])
    args = ap.parse_args()

    if not settings.llm.api_key:
        print("缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    db.init_db()

    todo = list(enumerate(SCENES["conversations"], start=1))
    if args.scene:
        todo = [t for t in todo if t[0] in args.scene]

    print(f"模型={settings.llm.model}  物流源={settings.logistics.provider}  场景数={len(todo)}")

    failed = 0
    total_checks = 0
    for idx, scene in todo:
        bad, checks, _ = await run_scene(scene, idx)
        total_checks += checks
        failed += bad

    print(f"\n{'=' * 78}")
    print(f"合计：{len(todo) - failed}/{len(todo)} 段对话合格（共 {total_checks} 项检查）")
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
