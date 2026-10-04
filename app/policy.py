"""发送许可策略。这是整套系统里最需要保守的一层。

原则：宁可少发一条，不可错发一条。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import load_allowed_chats, settings
from .schemas import Action, Intent


@dataclass
class PolicyVerdict:
    allowed: bool
    reason: str = ""


# 高风险操作词。这些事必须有人在系统里真的动手，绝不能靠模型自觉打标签。
# 模型偶尔会把"取消退回"标成 other，所以这道兜底不看 intent，只看文字。
HIGH_RISK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "退回/拦截": ("退回", "拦截", "退件", "拒收", "不要了", "别退", "取消退", "不退了", "退回来"),
    "改址改约": ("改址", "改地址", "改约", "换地址", "改到"),
    "理赔": ("理赔", "赔付", "赔偿", "索赔"),
    "丢件": ("丢件", "丢了", "件丢", "找不到件", "遗失"),
    "破损": ("破损", "压坏", "摔坏", "烂了", "坏掉"),
}


def high_risk_hit(text: str) -> str:
    """命中高风险操作词就返回类别名，否则返回空串。"""
    for label, words in HIGH_RISK_KEYWORDS.items():
        for w in words:
            if w in text:
                return label
    return ""


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def conversation_rule(channel: str, channel_chat_id: str) -> Optional[dict[str, Any]]:
    """从 chats.json 白名单里取该会话的规则。没登记 → None → 一律不接入。"""
    cfg = load_allowed_chats()
    for item in cfg.get("conversations", []):
        if item.get("channel") == channel and str(item.get("channel_chat_id")) == str(channel_chat_id):
            return item
    return None


def should_respond(msg, require_mention_in_group: bool = True) -> PolicyVerdict:
    """入站消息是否值得让 AI 开口。群聊默认只在被点名/带单号时介入。"""
    if msg.is_self:
        return PolicyVerdict(False, "自己的消息")
    if not msg.is_group:
        return PolicyVerdict(True)
    if msg.mentioned_bot:
        return PolicyVerdict(True)
    if not require_mention_in_group:
        return PolicyVerdict(True)
    if re.search(r"\d{8,}", msg.text):
        return PolicyVerdict(True, "含运单号")
    return PolicyVerdict(False, "群聊未被点名且无单号")


def check_send_policy(
    *,
    mode: str,
    takeover_until: Optional[str],
    action: Action,
    intent: Intent,
    reply: str,
    logistics_real: bool,
    auto_sent_last_minute: int,
    last_auto_sent_at: Optional[str],
    inbound_text: str = "",
    force: bool = False,
) -> PolicyVerdict:
    """能否把这条回复真正发出去。force=True 表示人工在审核台点了发送。"""
    if not reply.strip():
        return PolicyVerdict(False, "回复为空")

    if force:
        return PolicyVerdict(True, "人工审核后发送")

    # 第一道闸：文字里出现高风险操作词，一律人工。
    # 这道不依赖模型给的 intent —— 模型把"取消退回"标成 other 也拦得住。
    hit = high_risk_hit(f"{inbound_text}\n{reply}")
    if hit:
        return PolicyVerdict(False, f"命中高风险操作「{hit}」，一律人工处理")

    if mode != "auto":
        return PolicyVerdict(False, f"会话模式为 {mode}，不自动发送")

    if takeover_until and datetime.now(timezone.utc) < _parse_iso(takeover_until):
        return PolicyVerdict(False, "人工接管中")

    if action not in (Action.reply, Action.ask):
        return PolicyVerdict(False, f"{action.value} 不自动发送")

    # 需要人工在系统里真正动手的诉求，永远不自动发
    if intent in {
        Intent.intercept_return, Intent.cancel_return, Intent.change_address,
        Intent.claim, Intent.lost, Intent.damaged,
    }:
        return PolicyVerdict(False, "涉及人工在系统内实际操作的诉求")

    # 一切以物流状态为事实依据的答复，必须来自真实数据源
    if intent in {Intent.eta_inquiry, Intent.delivered_not_received, Intent.urge_delivery}:
        if not logistics_real:
            return PolicyVerdict(False, "物流数据源不是真实数据，禁止自动答复")

    if auto_sent_last_minute >= settings.auto_reply_max_per_minute:
        return PolicyVerdict(False, "触发频率上限")

    last = _parse_iso(last_auto_sent_at)
    if last is not None:
        delta = (datetime.now(timezone.utc) - last).total_seconds()
        if delta < settings.min_reply_interval_seconds:
            return PolicyVerdict(False, f"距上次自动回复仅 {delta:.1f}s，低于最小间隔")

    return PolicyVerdict(True, "允许自动发送")
