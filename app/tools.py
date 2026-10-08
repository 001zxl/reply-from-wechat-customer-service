"""模型可调用的工具。所有外部副作用都收敛在这里，方便审计和替换。

三类工具：
  1. query_logistics  —— 查运单轨迹（内置，走 logistics/ 下的服务商）
  2. call_<系统>       —— 调网点自己的"别的软件"（配置在 config/integrations.json）
  3. register_case    —— 登记人工待办（催派/拦截/改址这类有副作用的动作）

安全约定：只有 risk=read 的外部动作会真的被执行；
risk=write 的动作一律不代执行，只登记成待办转人工。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

from integrations import build_integrations
from logistics import get_provider

from . import db
from .prompts import TOOL_DEFINITIONS
from .schemas import ToolEvidence

log = logging.getLogger("tools")


@dataclass
class ToolOutcome:
    text: str                 # 回灌给模型的事实文本
    evidence: ToolEvidence
    logistics_ok: bool = False       # 一个真实物流结果是否成功返回
    logistics_real: bool = False     # 该结果是否来自真实数据源（非 mock）
    waybill_no: str = ""
    # ★ 这次工具结果是不是来自**模拟/演示**数据源。
    #   统一在这里标记，而不是只认 query_logistics —— 第一版只查了物流，
    #   于是"演示 ERP"返回的业务员、归属网点照样被当成真事实发出去
    #   （外部审查 P1）。任何来源标了 mock，整条草稿都不许外发。
    mock: bool = False


class ToolRegistry:
    def __init__(self, conversation_id: str, logistics_provider: Any | None = None,
                 integrations: Optional[list[Any]] = None,
                 allow_mock: Optional[bool] = None) -> None:
        self.conversation_id = conversation_id
        self.logistics = logistics_provider or get_provider()
        built = integrations if integrations is not None else build_integrations()
        # ★ 模拟/演示数据源默认**不注册给模型**（外部审查 P1）。
        #   想让模型能调（联调、演示），显式打 ALLOW_MOCK_TOOLS=1，
        #   或者在 chats.json 里把会话的 mode 设成 mock 通道。
        #   就算注册了，它的结果也会被 mock 标记拦住、永远不会真的发出去。
        from .config import settings as _s
        if allow_mock is None:
            allow_mock = bool(getattr(_s, "allow_mock_tools", False))
        self.integrations = [i for i in built
                             if allow_mock or not getattr(i, "is_mock", False)]
        dropped = len(built) - len(self.integrations)
        if dropped:
            log.info("已跳过 %d 个模拟/演示集成（未设 ALLOW_MOCK_TOOLS=1）", dropped)
        self._by_tool: dict[str, Any] = {
            f"call_{integ.system}": integ for integ in self.integrations
        }

    # ---------------- 工具定义 ----------------
    def definitions(self) -> list[dict[str, Any]]:
        defs = list(TOOL_DEFINITIONS)
        for integ in self.integrations:
            tool = self._system_tool(integ)
            if tool:
                defs.append(tool)
        return defs

    @staticmethod
    def _system_tool(integ: Any) -> Optional[dict[str, Any]]:
        """把一个外部系统做成一个工具，action 用 enum 限定。

        为什么按"系统"聚合而不是每个动作一个工具：网点动辄十几个动作，
        全摊开会让工具列表爆炸，模型反而选不准。按系统聚合 + 动作说明，
        实测路由更稳。
        """
        readable = [a for a in integ.actions() if a.callable_by_model]
        if not readable:
            return None

        enum = [a.name for a in readable]
        lines = [
            f"调用「{integ.label}」。{integ.description}".strip(),
            "可用动作：",
        ]
        for a in readable:
            lines.append(f"  - {a.name}（{a.label}）：{a.when or '按需调用'}")

        param_sets = [set(a.params) for a in readable]
        common = set.intersection(*param_sets) if param_sets else set()
        all_params = sorted(set().union(*param_sets)) if param_sets else []

        props: dict[str, Any] = {
            "action": {
                "type": "string",
                "enum": enum,
                "description": "要执行的动作",
            }
        }
        for p in all_params:
            props[p] = {
                "type": "string",
                "description": "运单号" if ("waybill" in p or p == "num") else p,
            }

        required = ["action"] + [p for p in all_params if p in common]
        return {
            "type": "function",
            "function": {
                "name": f"call_{integ.system}",
                "description": "\n".join(lines),
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }

    # ---------------- 执行 ----------------
    async def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        try:
            if name == "query_logistics":
                return await self._query_logistics(args)
            if name == "register_case":
                return self._register_case(args)
            if name in self._by_tool:
                return await self._call_system(name, args)
        except Exception as exc:
            # 工具自身出错不能把整轮对话打断，转成一个"事实不可用"的结果交回模型，
            # 模型会因此走 handoff，而不是凭空编一个答案。
            ev = ToolEvidence(
                tool=name, arguments=args, ok=False,
                summary=f"工具执行异常：{type(exc).__name__}",
            )
            return ToolOutcome(
                text=f"工具 {name} 执行失败（{type(exc).__name__}），没有拿到任何事实。"
                     "禁止据此编造结果，请转为人工处理。",
                evidence=ev,
            )
        ev = ToolEvidence(tool=name, arguments=args, ok=False, summary="未知工具")
        return ToolOutcome(text=f"错误：不存在名为 {name} 的工具。", evidence=ev)

    async def _call_system(self, tool_name: str, args: dict[str, Any]) -> ToolOutcome:
        integ = self._by_tool[tool_name]
        action = str(args.get("action") or "")
        params = {k: v for k, v in args.items() if k != "action"}

        spec = None
        for a in integ.actions():
            if a.name == action:
                spec = a
                break
        if spec is None:
            ev = ToolEvidence(tool=tool_name, arguments=args, ok=False,
                              summary=f"{integ.label} 没有动作 {action}")
            return ToolOutcome(
                text=f"错误：{integ.label} 没有名为 {action} 的动作。",
                evidence=ev,
            )

        # 有真实副作用的动作：不代执行，让模型去登记待办转人工
        if spec.risk == "write":
            ev = ToolEvidence(
                tool=tool_name, arguments=args, ok=False,
                summary=f"{integ.label}/{action} 属于人工操作，本系统不代执行",
            )
            return ToolOutcome(
                text=(
                    f"「{spec.label}」会产生真实后果（退回/改址这类），本系统**不会**代为执行。\n"
                    "请调用 register_case 把它登记成人工待办，并如实告诉对方需要人工处理。"
                ),
                evidence=ev,
            )

        # 命令类集成里 {python} 替换成当前解释器，避免依赖 PATH
        if hasattr(integ, "cfg"):
            for c in integ.cfg.get("commands", []):
                if c.get("name") == action:
                    c["argv"] = [str(x).replace("{python}", sys.executable)
                                 for x in c.get("argv", [])]

        result = await integ.run(action, params)
        is_mock = bool(getattr(integ, "is_mock", False))
        ev = ToolEvidence(
            tool=tool_name,
            arguments=args,
            ok=result.ok,
            summary=f"{integ.label}/{action} ok={result.ok}"
                    + (f" err={result.error[:80]}" if result.error else ""),
        )
        text = result.to_model_text()
        if is_mock and result.ok:
            # 明确告诉模型这是模拟数据 —— 它不该把演示数据当成真事实陈述
            text = ("【模拟数据源：仅供联调，不得作为对外答复依据】\n" + text)
        return ToolOutcome(text=text, evidence=ev, mock=is_mock)

    async def _query_logistics(self, args: dict[str, Any]) -> ToolOutcome:
        waybill = str(args.get("waybill_no") or "").strip()
        phone = str(args.get("phone_last4") or "").strip()
        if not waybill:
            ev = ToolEvidence(tool="query_logistics", arguments=args, ok=False, summary="缺少单号")
            return ToolOutcome(text="错误：未提供运单号。", evidence=ev)

        result = await self.logistics.query(waybill, phone)
        ev = ToolEvidence(
            tool="query_logistics",
            arguments=args,
            ok=result.ok and result.found,
            summary=(
                f"{result.source} / ok={result.ok} found={result.found} "
                f"state={result.state or '-'} err={result.error or '-'}"
            ),
        )
        real = bool(result.ok and result.found
                    and not result.source.lower().startswith("mock"))
        return ToolOutcome(
            text=result.to_model_text(),
            evidence=ev,
            logistics_ok=bool(result.ok and result.found),
            logistics_real=real,
            waybill_no=waybill,
            mock=bool(result.ok and result.found and not real),
        )

    def _register_case(self, args: dict[str, Any]) -> ToolOutcome:
        waybill = str(args.get("waybill_no") or "").strip()
        intent = str(args.get("intent") or "other").strip()
        note = str(args.get("note") or "").strip()
        case_id = db.upsert_case(self.conversation_id, waybill, intent, note)
        ev = ToolEvidence(
            tool="register_case", arguments=args, ok=True,
            summary=f"台账已记录 case#{case_id}",
        )
        return ToolOutcome(
            text=(
                f"已在网点内部台账记录：单号 {waybill or '(无)'}，意图 {intent}，说明：{note}。\n"
                "注意：这只是内部记录，尚未执行任何操作，回复中不得声称已催派/已拦截/已通知网点。"
            ),
            evidence=ev,
        )
