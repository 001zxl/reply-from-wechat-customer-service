"""大模型决策层：DeepSeek 官方 API（OpenAI 兼容）+ 工具调用循环 + 事实约束。

设计要点：
1. 模型可以主动调用 query_logistics 去查真实轨迹，然后再组织语言。
2. 涉及物流状态的意图，必须有成功的工具结果，否则强制转人工。
   这条是硬约束，不依赖模型自觉。
3. 单号必须在本轮证据里出现过，否则丢弃，防止模型串号。

已验证可用模型（均支持 function calling）：
  deepseek-v4-pro   理解力更强，客服默认用它
  deepseek-flash    更快更便宜，高并发时用
  deepseek-chat     兼容别名
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from .config import settings
from .schemas import (
    LOGISTICS_BOUND_INTENTS,
    WAYBILL_RE,
    Action,
    Decision,
    Intent,
    ToolEvidence,
)
from .tools import ToolRegistry

log = logging.getLogger("llm")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# 附在每条工具结果后面，把模型从"聊天模式"拽回"结构化输出模式"
JSON_NUDGE = (
    "\n\n（事实已获取完毕。现在请立即只输出那一个 JSON 对象，"
    "字段：action / intent / waybill_numbers / reply / handoff_reason。"
    "不要再输出任何解释文字，也不要再调用工具。）"
)

# "元话语"：模型在谈自己的格式/身份，而不是在回应客户。
# 实测出现过一次 "收到，后续我会按格式返回。" —— 这种话发给商家是灾难。
META_RE = re.compile(
    # 谈"格式"。注意不能只写"按…格式" —— 那会把"你按这个格式发我：单号+问题"
    # 这种正常的客服话术也误判掉。必须带上自我指涉的输出动词。
    r"格式\s*(回复|返回|输出|答复)|"
    # 自我身份
    r"作为(一个)?(AI|人工智能|语言模型|助手程序|智能助手)|"
    # 谈系统指令
    r"系统(提示|指令|要求)|"
    # "收到/好的，以后就按…"
    r"(收到|好的?|明白)[了]?[，,]?\s*(以后|下次)(就)?按|"
    r"收到[，,]\s*(后续|我将|我会|已按)|"
    # 明显是在输出结构而不是说话
    r"\bJSON\b|字段[:：]"
)

REPAIR_INSTRUCTION = (
    "上面的回复格式不对。请只输出那一个 JSON 对象，"
    "字段：action / intent / waybill_numbers / reply / handoff_reason。"
    "action 只能是 reply/ask/handoff/wait。不要输出任何其他文字。"
)


@dataclass
class LLMOutcome:
    decision: Decision
    evidence: list[ToolEvidence] = field(default_factory=list)
    tool_rounds: int = 0
    model: str = ""
    ok: bool = True
    error: str = ""
    logistics_real: bool = False
    latency_ms: int = 0


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。模型偶尔会加 ``` 或前后废话，这里兜住。"""
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("模型输出中没有 JSON 对象")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : i + 1])
    raise ValueError("模型输出的 JSON 不完整")


def looks_like_meta(reply: str) -> bool:
    """回复是不是在讲模型自己的格式/身份，而不是回应用户。"""
    return bool(META_RE.search(reply or ""))


def _force_handoff(decision: Decision, reason: str) -> Decision:
    decision.action = Action.handoff
    decision.handoff_reason = (decision.handoff_reason or reason)[:300]
    if not decision.reply.strip():
        decision.reply = "这个我需要跟网点那边核实一下再回你，稍等我确认。"
    return decision


class CustomerServiceLLM:
    """真实模型。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None) -> None:
        cfg = settings.llm
        self.model = model or cfg.model
        self.timeout = cfg.timeout
        self.max_tool_rounds = cfg.max_tool_rounds
        # 温度和输出上限跟着当前模型档案走（不同厂商的最佳值不一样）
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        key = api_key or cfg.api_key
        if not key:
            from . import models

            st = models.status()
            hint = "；".join(
                f"{p['label']}（{p['id']}）：{'，'.join(p['problems'])}"
                for p in st["not_configured"][:3]
            ) or "config/models.json 里没有启用任何模型"
            raise RuntimeError(
                f"当前模型没有可用的 API Key。\n{hint}\n"
                f"解决办法：在 .env 里填上对应密钥，或在 /desk 换成已配好的模型。"
            )
        self.client = AsyncOpenAI(
            api_key=key,
            base_url=base_url or cfg.base_url,
            timeout=self.timeout,
            max_retries=0,          # 失败就直接转人工，不做重试风暴
        )

    async def _chat(self, **kwargs: Any):
        """调模型。连接类错误重试一次 —— 网络抖一下不该直接转人工。

        只重试一次，不做重试风暴；而且重建客户端，
        避免复用到已经失效的连接。
        """
        for attempt in range(2):
            try:
                return await self.client.chat.completions.create(**kwargs)
            except (APIConnectionError, APITimeoutError):
                if attempt == 1:
                    raise
                log.warning("模型连接异常，重建客户端重试一次")
                self.client = AsyncOpenAI(
                    api_key=self.client.api_key,
                    base_url=str(self.client.base_url),
                    timeout=self.timeout,
                    max_retries=0,
                )
                await asyncio.sleep(0.6)

    async def decide(
        self,
        *,
        conversation_id: str,
        batch_id: str,
        history: list[dict[str, str]],
        batch_text: str,
        context_block: str,
        system_prompt: str,
        tools: Optional[ToolRegistry] = None,
    ) -> LLMOutcome:
        started = time.time()
        registry = tools or ToolRegistry(conversation_id)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_block}"},
        ]
        messages.extend(history[-20:])
        messages.append({"role": "user", "content": batch_text})

        evidence: list[ToolEvidence] = []
        tool_texts: list[str] = []
        logistics_real = False
        rounds = 0
        final_text = ""

        try:
            for _ in range(self.max_tool_rounds):
                resp = await self._chat(
                    model=self.model,
                    messages=messages,
                    tools=registry.definitions(),
                    temperature=self.temperature,
                    # 思考模型（如 deepseek-v4-pro）的 reasoning token 也算在这个预算里。
                    # 给太小会把 JSON 截断（finish_reason=length），所以按档案留足余量。
                    max_tokens=self.max_tokens,
                )
                choice = resp.choices[0]
                msg = choice.message

                if choice.finish_reason == "tool_calls" and msg.tool_calls:
                    rounds += 1
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "{}",
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    })
                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                            if not isinstance(args, dict):
                                args = {}
                        except json.JSONDecodeError:
                            args = {}
                        outcome = await registry.execute(tc.function.name, args)
                        evidence.append(outcome.evidence)
                        tool_texts.append(outcome.text)
                        if outcome.logistics_real:
                            logistics_real = True
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            # 关键：把"接下来必须输出 JSON"顶到模型眼前。
                            # 实测不这么做，模型拿到工具结果后会改用自然语言回答。
                            "content": outcome.text + JSON_NUDGE,
                        })
                    continue

                if choice.finish_reason not in ("stop", "length", None):
                    raise ValueError(f"模型输出未正常结束：{choice.finish_reason}")
                if choice.finish_reason == "length":
                    # 被 max_tokens 截断了，内容大概率不是合法 JSON。
                    # 不直接判死，走下面的 repair 用 json_object 强约束重问一次。
                    log.warning("模型输出被 max_tokens 截断，转 repair 兜底")
                final_text = msg.content or ""
                break
            else:
                raise ValueError("工具调用轮次超限")

            if not final_text.strip():
                raise ValueError("模型返回了空内容")

            try:
                decision = Decision.model_validate(extract_json(final_text))
            except (ValueError, json.JSONDecodeError):
                # 兜底：模型偶尔仍会用自然语言收尾。用一次强约束重问，
                # 只在解析失败时才付这次调用。
                repaired = await self._repair_json(messages, final_text)
                decision = Decision.model_validate(extract_json(repaired))

            if looks_like_meta(decision.reply):
                # 模型在讲自己的格式而不是回应用户，重问一次
                log.warning("回复疑似元话语，重问：%r", decision.reply[:60])
                try:
                    repaired = await self._repair_json(messages, decision.reply)
                    retry = Decision.model_validate(extract_json(repaired))
                    if not looks_like_meta(retry.reply):
                        decision = retry
                except Exception:
                    log.exception("元话语重问失败")
                if looks_like_meta(decision.reply):
                    decision = _force_handoff(decision, "模型输出异常，需人工处理")

            decision = self._enforce_grounding(decision, evidence, history, batch_text, tool_texts)

            return LLMOutcome(
                decision=decision, evidence=evidence, tool_rounds=rounds,
                model=self.model, ok=True, logistics_real=logistics_real,
                latency_ms=int((time.time() - started) * 1000),
            )
        except Exception as exc:
            return LLMOutcome(
                decision=Decision(
                    action=Action.handoff,
                    intent=Intent.other,
                    reply="我这边系统刚有点异常，这个我先记下来交给同事跟进，确认后回你。",
                    handoff_reason=f"模型调用失败：{type(exc).__name__}",
                ),
                evidence=evidence, tool_rounds=rounds, model=self.model,
                ok=False, error=f"{type(exc).__name__}: {exc}"[:300],
                logistics_real=logistics_real,
                latency_ms=int((time.time() - started) * 1000),
            )

    # ------------------------------------------------------------------
    async def _repair_json(self, messages: list[dict[str, Any]], bad_text: str) -> str:
        """解析失败时的兜底：用 json_object 强约束重问一次。"""
        repair_messages = list(messages) + [
            {"role": "assistant", "content": (bad_text or "(空)")[:1500]},
            {"role": "user", "content": REPAIR_INSTRUCTION},
        ]
        resp = await self._chat(
            model=self.model,
            messages=repair_messages,
            temperature=0.0,
            # 和主调用一样要给足：思考模型的 reasoning token 也算在这里面，
            # 给 1500 会被推理吃光，content 直接是空的 —— 兜底就白兜了。
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        choice = resp.choices[0]
        content = choice.message.content or ""
        if not content.strip():
            detail = getattr(choice, "finish_reason", "?")
            usage = getattr(resp, "usage", None)
            log.error("兜底重问返回空内容 finish_reason=%s usage=%s", detail, usage)
        return content

    def _enforce_grounding(
        self,
        decision: Decision,
        evidence: list[ToolEvidence],
        history: list[dict[str, str]],
        batch_text: str,
        tool_texts: list[str],
    ) -> Decision:
        if decision.action == Action.wait:
            decision.reply = ""
            return decision

        if not decision.reply.strip():
            return _force_handoff(decision, "模型未给出回复内容")

        # 1) 单号必须有出处（用户说过，或工具返回过）
        allowed_text = "\n".join(
            [h["content"] for h in history] + [batch_text] + tool_texts
        )
        kept = [n for n in decision.waybill_numbers if n in allowed_text]
        dropped = [n for n in decision.waybill_numbers if n not in allowed_text]
        decision.waybill_numbers = kept
        if dropped:
            decision.handoff_reason = (
                decision.handoff_reason or f"模型引用了上下文中不存在的单号：{','.join(dropped)}"
            )[:300]

        # 2) 涉及**具体单号**的物流状态，但没有真实查询结果 → 强制人工。
        #    注意这里要求"确实有单号"：像"杭州到南京几天到"这种一般性时效咨询
        #    本来就查不到轨迹，不该被这条规则打成转人工。
        query_ok = any(e.tool == "query_logistics" and e.ok for e in evidence)
        has_waybill = bool(decision.waybill_numbers) or bool(
            WAYBILL_RE.search(batch_text)
        )
        if (decision.intent in LOGISTICS_BOUND_INTENTS
                and decision.action == Action.reply
                and has_waybill and not query_ok):
            return _force_handoff(
                decision,
                "涉及具体单号的物流状态，但本轮没有成功的查询结果，需人工核实",
            )
        return decision


def build_llm() -> CustomerServiceLLM:
    return CustomerServiceLLM()
