"""统一数据契约：进来的消息、模型的决定、物流查询结果。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 单号：申通常见为 12~15 位数字，但也可能是字母数字混合的电商单号
WAYBILL_RE = re.compile(r"\b(?=[0-9A-Za-z-]{8,24}\b)(?=[0-9A-Za-z-]*\d)[0-9A-Za-z][0-9A-Za-z-]{7,23}\b")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Action(str, Enum):
    reply = "reply"        # 可以回答
    ask = "ask"            # 信息不足，需要追问
    handoff = "handoff"    # 需要人工处理
    wait = "wait"          # 不是对助手的请求 / 不该插话


class ConversationMode(str, Enum):
    off = "off"            # 只保留人工
    review = "review"      # AI 出草稿，人工审核后发送
    auto = "auto"          # 合规规则内自动发送


class Intent(str, Enum):
    urge_delivery = "urge_delivery"                      # 催派
    intercept_return = "intercept_return"                # 拦截退回
    cancel_return = "cancel_return"                      # 取消退回，继续送
    change_address = "change_address"                    # 改址/改约
    delivered_not_received = "delivered_not_received"    # 签收未收到
    damaged = "damaged"                                  # 破损
    lost = "lost"                                        # 丢件
    claim = "claim"                                      # 理赔
    eta_inquiry = "eta_inquiry"                          # 时效咨询
    business_inquiry = "business_inquiry"                # 业务/价格/规则咨询
    social = "social"                                    # 闲聊、谢谢、表情
    other = "other"


# 这些意图必须基于真实物流工具结果才能答复；否则强制转人工
LOGISTICS_BOUND_INTENTS = {
    Intent.urge_delivery,
    Intent.intercept_return,
    Intent.cancel_return,
    Intent.change_address,
    Intent.delivered_not_received,
    Intent.damaged,
    Intent.lost,
    Intent.claim,
    Intent.eta_inquiry,
}


class IncomingMessage(BaseModel):
    """适配层 → 编排层的统一消息。"""

    event_id: str = Field(min_length=6, max_length=200, description="渠道原生消息ID；没有就用适配层持久化序号拼出的稳定ID")
    channel: str
    conversation_id: str = Field(min_length=1, max_length=200, description="本系统内部会话ID")
    channel_chat_id: str = Field(min_length=1, max_length=300, description="渠道侧会话标识，发送时必须回传校验")
    sender_id: str
    sender_name: str = ""
    text: str = Field(min_length=1, max_length=4000)
    is_group: bool = False
    is_self: bool = False
    mentioned_bot: bool = False
    # 非文字消息：kind 是 "image"/"video"，box 是截图内的像素框 (x0,y0,x1,y1)
    media: str = ""
    media_box: tuple[int, int, int, int] | tuple = ()
    media_path: str = ""
    received_at: str = Field(default_factory=now_iso)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class Decision(BaseModel):
    action: Action
    intent: Intent = Intent.other
    waybill_numbers: list[str] = Field(default_factory=list, max_length=30)
    reply: str = Field(default="", max_length=800)
    handoff_reason: str = Field(default="", max_length=300)

    @field_validator("waybill_numbers")
    @classmethod
    def _clean_waybills(cls, v: list[str]) -> list[str]:
        out, seen = [], set()
        for item in v:
            s = (item or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out


class LogisticsEvent(BaseModel):
    time: str
    status: str
    location: str = ""


class LogisticsResult(BaseModel):
    """归一化后的物流结果。ok=False 时绝不能对外声称任何状态。"""

    ok: bool
    found: bool = False
    waybill_no: str = ""
    carrier: str = ""
    state: str = ""              # 在途/派件中/已签收/退回/异常...
    signed: bool = False
    latest_time: str = ""
    latest_status: str = ""
    events: list[LogisticsEvent] = Field(default_factory=list)
    source: str = ""             # kuaidi100 / sto / mock
    error: str = ""
    raw_digest: str = ""

    def to_model_text(self, limit: int = 6) -> str:
        """给模型看的紧凑事实。只放事实，不放任何建议。"""
        if not self.ok:
            return f"查询失败：{self.error or '未知错误'}。禁止对外声称任何物流状态。"
        if not self.found:
            return f"单号 {self.waybill_no} 在 {self.carrier} 未查到有效轨迹。禁止编造。"
        lines = [
            f"单号：{self.waybill_no}（{self.carrier}）",
            f"当前状态：{self.state}；是否已签收：{'是' if self.signed else '否'}",
            f"最新节点：{self.latest_time} {self.latest_status}",
            "轨迹：",
        ]
        for ev in self.events[-limit:]:
            lines.append(f"  - {ev.time} {ev.status} {ev.location}".rstrip())
        lines.append(f"数据来源：{self.source}；查询时间：{now_iso()}")
        return "\n".join(lines)


class ToolEvidence(BaseModel):
    """一次工具调用的留痕，用于事后审计与前端展示。"""

    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
