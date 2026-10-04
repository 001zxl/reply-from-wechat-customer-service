#!/usr/bin/env python3
"""真实模型契约测试：验证"理解能力 + 工具调用 + 事实约束"。

使用 DeepSeek 官方 API，需要先配置 DEEPSEEK_API_KEY（环境变量或 .env），
会产生真实计费。

    python tests/run_cases.py            # 跑全部用例
    python tests/run_cases.py --case 1   # 只跑第 1 条
    DEEPSEEK_MODEL=deepseek-flash python tests/run_cases.py   # 换模型
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

# 用独立的临时库，避免污染生产 data/assistant.db
os.environ.setdefault("DB_PATH", "data/run_cases.db")

from app import db, policy  # noqa: E402
from app.config import load_knowledge, settings  # noqa: E402
from app.llm import CustomerServiceLLM  # noqa: E402
from app.prompts import SYSTEM_PROMPT, build_context_block  # noqa: E402
from app.tools import ToolRegistry  # noqa: E402

CASES = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))

# 出现在禁止短语附近的否定词，说明模型是在"拒绝"，不是"照做"
NEGATIONS = ["不", "没", "别", "无法", "拒绝", "编不了", "不能", "没法", "打包票", "不敢", "禁止"]


def violates(reply: str, phrase: str, window: int = 16) -> bool:
    """禁止短语是否被"正面使用"。

    模型说"但'今天肯定能到'我不能打包票"是合规的拒绝；
    只有附近没有否定词时才算真的违规。
    """
    start = 0
    while True:
        idx = reply.find(phrase, start)
        if idx < 0:
            return False
        around = reply[max(0, idx - window) : idx + len(phrase) + window]
        if not any(neg in around for neg in NEGATIONS):
            return True
        start = idx + len(phrase)


async def run_one(idx: int, case: dict) -> bool:
    llm = CustomerServiceLLM()
    context = build_context_block(
        title=case.get("title", "测试会话"),
        is_group=bool(case.get("is_group")),
        merchant=case.get("merchant", "测试商家"),
        knowledge=load_knowledge(),
        open_cases=case.get("open_cases", []),
    )
    registry = ToolRegistry(case.get("conversation_id", f"case-{idx}"))
    outcome = await llm.decide(
        conversation_id=case.get("conversation_id", f"case-{idx}"),
        batch_id=f"case{idx}",
        history=case.get("history", []),
        batch_text=case["text"],
        context_block=context,
        system_prompt=SYSTEM_PROMPT,
        tools=registry,
    )

    d = outcome.decision
    print(f"\n{'=' * 72}\n用例 {idx}：{case.get('name', '')}\n{'=' * 72}")
    print(f"输入           : {case['text']!r}")
    print(f"模型可用       : {outcome.ok}  轮次={outcome.tool_rounds} 耗时={outcome.latency_ms}ms")
    if outcome.error:
        print(f"错误           : {outcome.error}")
    print(f"action         : {d.action.value}")
    print(f"intent         : {d.intent.value}")
    print(f"单号           : {d.waybill_numbers}")
    print(f"回复           : {d.reply}")
    print(f"转人工原因     : {d.handoff_reason}")
    print(f"物流数据真实   : {outcome.logistics_real}")
    for e in outcome.evidence:
        print(f"  工具 {e.tool} ok={e.ok} :: {e.summary}")

    problems: list[str] = []
    if not outcome.ok:
        problems.append("模型调用失败")
    expect_action = case.get("expect_action")
    if expect_action and d.action.value not in expect_action:
        problems.append(f"期望 action ∈ {expect_action}，实际 {d.action.value}")
    expect_intent = case.get("expect_intent")
    if expect_intent and d.intent.value not in expect_intent:
        problems.append(f"期望 intent ∈ {expect_intent}，实际 {d.intent.value}")
    for bad in case.get("forbid_in_reply", []):
        if violates(d.reply, bad):
            problems.append(f"回复中出现了禁止表述：{bad}")
    for need in case.get("must_in_reply", []):
        if need not in d.reply:
            problems.append(f"回复中缺少必需内容：{need}")
    if case.get("expect_blocked_in_auto"):
        verdict = policy.check_send_policy(
            mode="auto", takeover_until=None, action=d.action, intent=d.intent,
            reply=d.reply, logistics_real=outcome.logistics_real,
            auto_sent_last_minute=0, last_auto_sent_at=None,
            inbound_text=case["text"],
        )
        if verdict.allowed:
            problems.append("这条在 auto 模式下会被自动发出，但它必须人工处理")

    any_of = case.get("must_match_any", [])
    if any_of and not any(x in d.reply for x in any_of):
        problems.append(f"回复中应当包含以下任一表述：{any_of}")

    if problems:
        print("\n结果           : 不合格")
        for p in problems:
            print(f"  - {p}")
        return False
    print("\n结果           : 合格")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=int, default=0, help="只跑指定序号（从 1 开始）")
    args = parser.parse_args()

    if not settings.llm.api_key:
        print(
            "缺少 DEEPSEEK_API_KEY，请先 export DEEPSEEK_API_KEY=sk-xxx 或写入 .env。",
            file=sys.stderr,
        )
        return 2

    db.init_db()

    todo = list(enumerate(CASES, start=1))
    if args.case:
        todo = [t for t in todo if t[0] == args.case]
    print(f"模型={settings.llm.model}  用例数={len(todo)}  物流源={settings.logistics.provider}")

    passed = 0
    for idx, case in todo:
        if await run_one(idx, case):
            passed += 1
    print(f"\n{'=' * 72}\n合计：{passed}/{len(todo)} 合格\n{'=' * 72}")
    return 0 if passed == len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
