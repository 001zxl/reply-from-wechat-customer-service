#!/usr/bin/env python3
"""业务流程测试：验证"听懂诉求 → 调对工具 → 给准确答复"。

跟 run_cases.py 的区别：
- run_cases 测的是单点理解（指代、注入、闲聊）
- 这个测的是**业务闭环**：该不该查、查了没、答得对不对、有没有编事实

用 mock 物流源，结果可复现，不花钱调外部接口。

    python tests/run_business.py              # 跑全部
    python tests/run_business.py --case 3     # 只跑第 3 条
    python tests/run_business.py --group 查询类
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

os.environ.setdefault("DB_PATH", "data/business_test.db")
os.environ.setdefault("LOGISTICS_PROVIDER", "mock")
os.environ.setdefault("DRY_RUN", "0")

# 测试要固定结果：关掉夜间静默、随机延迟、打字模拟，否则同一个用例
# 在白天和半夜跑会得到不同结论（风控本来就该这样，但测试需要确定性）
os.environ["QUIET_HOURS"] = ""
os.environ["REPLY_DELAY_MIN"] = "0"
os.environ["REPLY_DELAY_MAX"] = "0"
os.environ["TYPING_SIMULATION"] = "0"

from app import db  # noqa: E402
from app.config import load_knowledge, settings  # noqa: E402
from app.llm import CustomerServiceLLM, looks_like_meta  # noqa: E402
from app.prompts import SYSTEM_PROMPT, build_context_block  # noqa: E402
from app.tools import ToolRegistry  # noqa: E402

CASES = json.loads((Path(__file__).parent / "business_cases.json").read_text(encoding="utf-8"))

# 物流状态词。回复里出现这些词时，必须和工具真实返回的状态一致。
STATE_WORDS = [
    "已签收", "派件中", "派送中", "运输中", "在途", "已揽收", "退回中",
    "疑难件", "拒签", "退签", "清关中", "派送失败",
]

NEGATIONS = ["不", "没", "别", "无法", "拒绝", "不能", "没法", "不敢", "尚未", "还未"]

# 同一个意思的多种说法。断言要测语义，不是测字符串 ——
# 模型说"已经在派件了"和"派件中"是一回事，不该判不合格。
STATE_ALIASES: dict[str, list[str]] = {
    "派件中": ["派件中", "派件了", "正在派件", "派送中", "正在派送", "派送环节", "在派送"],
    "已签收": ["已签收", "已经签收", "签收了", "显示签收", "已妥投", "妥投"],
    "运输中": ["运输中", "在途", "在路上", "转运中"],
    # 注意：不要放裸的"退回"。回复里说"退回拦截需要核实"是在讲操作名，
    # 不是在声称物流状态，放了会误报。
    "退回中": ["退回中", "退件中", "已在退", "已退回"],
    "已揽收": ["已揽收", "已收件", "揽收了"],
    "疑难件": ["疑难件", "疑难"],
    "拒签": ["拒签", "拒收"],
    "派送失败": ["派送失败", "派件失败"],
}


def state_present(reply: str, state: str) -> bool:
    """回复里有没有表达出这个状态（容忍同义说法）。"""
    for alias in STATE_ALIASES.get(state, [state]):
        if alias in reply:
            return True
    return False


def canonical_states(reply: str) -> set[str]:
    """回复里提到了哪些状态（归一化后）。"""
    out = set()
    for state in STATE_ALIASES:
        if state_present(reply, state):
            out.add(state)
    return out


def waybill_present(reply: str, waybill: str) -> bool:
    """回复里有没有指向这个单号。

    人一般不重复整串单号，会说"尾号1098这票"。所以除了完整单号，
    也认"尾号+后四位"和完整的后八位。
    """
    if not waybill:
        return True
    if waybill in reply:
        return True
    tail4, tail8 = waybill[-4:], waybill[-8:]
    if f"尾号{tail4}" in reply or f"尾号 {tail4}" in reply:
        return True
    return tail8 in reply
ASK_MARKERS = ["发我", "提供", "给我", "是哪", "哪个", "多少", "方便说", "发一下", "告诉我是"]


def violates(reply: str, phrase: str, window: int = 16) -> bool:
    """禁止短语是否被正面使用（拒绝语境不算违规）。"""
    start = 0
    while True:
        idx = reply.find(phrase, start)
        if idx < 0:
            return False
        around = reply[max(0, idx - window): idx + len(phrase) + window]
        if not any(neg in around for neg in NEGATIONS):
            return True
        start = idx + len(phrase)


def reasks(reply: str, keyword: str, window: int = 14) -> bool:
    """回复是不是在"重新索要"某个已经给过的信息。"""
    start = 0
    while True:
        idx = reply.find(keyword, start)
        if idx < 0:
            return False
        around = reply[max(0, idx - window): idx + len(keyword) + window]
        if any(m in around for m in ASK_MARKERS):
            return True
        start = idx + len(keyword)


def states_from_evidence(evidence: list[dict]) -> set[str]:
    """从工具证据里抠出真实物流状态。"""
    found: set[str] = set()
    for e in evidence:
        summary = str(e.get("summary") or "")
        for part in summary.split():
            if part.startswith("state="):
                value = part.split("=", 1)[1]
                if value and value != "-":
                    found.add(value)
    return found


def check_facts(reply: str, evidence: list[dict]) -> list[str]:
    """事实一致性：回复里说的物流状态必须来自工具的真实返回。"""
    real = states_from_evidence(evidence)
    if not real:
        return []
    said = canonical_states(reply)
    if not said:
        return []
    # 归一化后比对：回复里提到的状态必须是工具真实返回过的
    wrong = {s for s in said if s not in real}
    if len(real) == 1 and wrong:
        return [f"回复说的是 {sorted(said)}，但工具实际返回的是 {sorted(real)}"]
    # 多票场景：只要回复里没有'工具完全没提过'的状态就行
    return [f"回复提到了工具从未返回的状态：{sorted(wrong)}"] if len(wrong) > 1 else []


async def run_one(idx: int, case: dict) -> bool:
    llm = CustomerServiceLLM()
    context = build_context_block(
        title=case.get("name", "业务测试"),
        is_group=bool(case.get("is_group")),
        merchant=case.get("merchant", "测试商家"),
        knowledge=load_knowledge(),
        open_cases=case.get("open_cases", []),
    )
    registry = ToolRegistry(case.get("conversation_id", f"biz-{idx}"))
    outcome = await llm.decide(
        conversation_id=case.get("conversation_id", f"biz-{idx}"),
        batch_id=f"biz{idx}",
        history=case.get("history", []),
        batch_text=case["text"],
        context_block=context,
        system_prompt=SYSTEM_PROMPT,
        tools=registry,
    )

    d = outcome.decision
    called = [e.tool for e in outcome.evidence]
    print(f"\n{'=' * 74}")
    print(f"[{idx:02d}] {case['name']}   《{case.get('group', '')}》")
    print(f"{'=' * 74}")
    print(f"输入     : {case['text'][:78]}")
    print(f"动作     : {d.action.value}  |  意图 {d.intent.value}  |  单号 {d.waybill_numbers}")
    print(f"调用工具 : {called or '（没有）'}")
    print(f"回复     : {d.reply}")
    if d.handoff_reason:
        print(f"转人工   : {d.handoff_reason}")
    if outcome.error:
        print(f"错误     : {outcome.error}")

    problems: list[str] = []
    if not outcome.ok:
        problems.append("模型调用失败")

    if (exp := case.get("expect_action")) and d.action.value not in exp:
        problems.append(f"动作应为 {exp}，实际 {d.action.value}")

    for need in case.get("must_call_tools", []):
        if need not in called:
            problems.append(f"应当调用 {need}，实际只调了 {called or '无'}")
    for bad in case.get("must_not_call_tools", []):
        if bad in called:
            problems.append(f"不该调用 {bad}，但调了")

    if (wb := case.get("must_mention_waybill")) and not waybill_present(d.reply, wb):
        problems.append(f"回复里没有指向单号 {wb}（完整号或尾号都算）")

    for bad in case.get("forbid_in_reply", []):
        if violates(d.reply, bad):
            problems.append(f"出现了不该有的表述：「{bad}」")

    for kw in case.get("must_not_ask_for", []):
        if reasks(d.reply, kw):
            problems.append(f"重复索要已给过的信息：「{kw}」")

    want_state = case.get("must_reflect_state")
    if want_state:
        for w in ([want_state] if isinstance(want_state, str) else want_state):
            if not state_present(d.reply, w):
                problems.append(f"回复里没有体现真实状态「{w}」")

    # 元话语：模型在讲自己的格式/身份，而不是回应用户。发给商家是灾难。
    if looks_like_meta(d.reply):
        problems.append(f"回复是元话语，不是在回应客户：{d.reply[:60]!r}")

    problems.extend(check_facts(d.reply, [e.model_dump() for e in outcome.evidence]))

    if problems:
        print("\n结果     : ✗ 不合格")
        for p in problems:
            print(f"           · {p}")
        print(f"           理由：{case.get('why', '')}")
        return False
    print("\n结果     : ✓ 合格")
    return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, action="append", default=[])
    ap.add_argument("--group", default="")
    args = ap.parse_args()

    if not settings.llm.api_key:
        print("缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    db.init_db()

    todo = list(enumerate(CASES, start=1))
    if args.case:
        todo = [t for t in todo if t[0] in args.case]
    if args.group:
        todo = [t for t in todo if t[1].get("group") == args.group]

    print(f"模型={settings.llm.model}  物流源={settings.logistics.provider}  用例={len(todo)}")

    passed = 0
    failed_names: list[str] = []
    for idx, case in todo:
        if await run_one(idx, case):
            passed += 1
        else:
            failed_names.append(f"[{idx:02d}] {case['name']}")

    print(f"\n{'=' * 74}")
    print(f"合计：{passed}/{len(todo)} 合格")
    if failed_names:
        print("未通过：")
        for n in failed_names:
            print(f"  · {n}")
    print("=" * 74)
    return 0 if passed == len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
