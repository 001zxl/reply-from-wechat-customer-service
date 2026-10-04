"""离线用的假模型：不花钱、不联网，用来验证编排、去重、接管、发送确认这些逻辑。

它不是"模拟智能"，只是把最典型的几种情况写成规则，让整条流水线能被测试。
真实理解能力必须用 CustomerServiceLLM 测（见 tests/run_cases.py --live）。
"""

from __future__ import annotations

import re
from typing import Optional

from .llm import LLMOutcome
from .schemas import WAYBILL_RE, Action, Decision, Intent
from .tools import ToolRegistry

GREETING = ("谢谢", "多谢", "好的", "收到", "嗯", "ok", "OK", "辛苦了", "麻烦了", "在吗")

INTENT_RULES = [
    (Intent.cancel_return, ("别退", "不要退", "取消退", "继续送", "又要了", "不退了", "正常派送")),
    (Intent.intercept_return, ("拦截", "退回", "退回来", "拒收", "退件", "不要了")),
    (Intent.change_address, ("改地址", "改址", "换个地址", "改约", "改到")),
    (Intent.delivered_not_received, ("签收", "没收到", "未收到", "没拿到")),
    (Intent.damaged, ("破损", "坏了", "压坏", "烂了")),
    (Intent.lost, ("丢了", "找不到了", "丢件", "不见了")),
    (Intent.claim, ("理赔", "赔偿", "赔付")),
    (Intent.urge_delivery, ("催", "快点", "加急", "什么时候到", "还没到", "催派", "时效")),
    (Intent.business_inquiry, ("多少钱", "价格", "怎么收费", "首重", "续重", "能不能寄", "规则")),
]


class MockLLM:
    def __init__(self) -> None:
        self.model = "mock-llm"

    async def decide(
        self,
        *,
        conversation_id: str,
        batch_id: str,
        history: list[dict[str, str]],
        batch_text: str,
        context_block: str = "",
        system_prompt: str = "",
        tools: Optional[ToolRegistry] = None,
    ) -> LLMOutcome:
        registry = tools or ToolRegistry(conversation_id)
        evidence = []
        tool_texts = []
        logistics_real = False

        stripped = batch_text.strip()
        if any(stripped == g or stripped.startswith(g) for g in GREETING) and len(stripped) <= 8:
            return LLMOutcome(
                decision=Decision(action=Action.wait, intent=Intent.social, reply=""),
                model=self.model,
            )

        numbers = list(dict.fromkeys(WAYBILL_RE.findall(batch_text)))
        history_numbers = [
            n for h in history for n in WAYBILL_RE.findall(h["content"])
        ]
        all_numbers = list(dict.fromkeys(history_numbers + numbers))

        intent = Intent.other
        for cand, keys in INTENT_RULES:
            if any(k in batch_text for k in keys):
                intent = cand
                break

        if intent in (Intent.business_inquiry, Intent.other) and not numbers:
            return LLMOutcome(
                decision=Decision(
                    action=Action.ask, intent=Intent.business_inquiry,
                    reply="你说的这个我需要跟网点确认下口径，方便说一下具体是哪个商家、哪票货吗？",
                ),
                model=self.model,
            )

        # 指代不清：历史里有多票，本次没有明确单号，且用了指代词
        if all_numbers and not numbers and len(all_numbers) > 1 and re.search(
            r"这票|那个|这个|刚才|上面|它", batch_text
        ):
            return LLMOutcome(
                decision=Decision(
                    action=Action.ask, intent=intent,
                    waybill_numbers=all_numbers,
                    reply=f"你这边有 {len(all_numbers)} 票，"
                          + "、".join(all_numbers[-3:])
                          + "，说的是哪一票？",
                ),
                model=self.model,
            )

        target = (numbers or all_numbers)[:1]
        if target and intent not in (Intent.business_inquiry, Intent.other):
            outcome = await registry.execute("query_logistics", {"waybill_no": target[0]})
            evidence.append(outcome.evidence)
            tool_texts.append(outcome.text)
            logistics_real = outcome.logistics_real
            head = "查到" if outcome.logistics_ok else "暂时没查到"
            reply = f"{target[0]} 这边{head}：{outcome.text.splitlines()[2] if outcome.logistics_ok else '系统没返回轨迹'}。"
            decision = Decision(
                action=Action.handoff if intent in (
                    Intent.intercept_return, Intent.cancel_return, Intent.change_address,
                    Intent.claim, Intent.lost, Intent.damaged,
                ) else Action.reply,
                intent=intent,
                waybill_numbers=target,
                reply=reply + "需要网点那边核实后给你准信。",
                handoff_reason="" if intent not in (
                    Intent.intercept_return, Intent.cancel_return, Intent.change_address,
                    Intent.claim, Intent.lost, Intent.damaged,
                ) else "涉及实际操作，需人工在系统内执行",
            )
            return LLMOutcome(
                decision=decision, evidence=evidence, tool_rounds=1,
                model=self.model, logistics_real=logistics_real,
            )

        return LLMOutcome(
            decision=Decision(
                action=Action.ask, intent=intent,
                reply="麻烦把运单号发我一下，我这边直接查。",
            ),
            model=self.model,
        )
