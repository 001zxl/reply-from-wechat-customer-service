"""发送许可策略。这是整套系统里最需要保守的一层。

原则：宁可少发一条，不可错发一条。
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime as _dt
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


_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """当前是不是在夜间静默时段。支持跨午夜（22:00-08:00）。"""
    spec = (settings.quiet_hours or "").strip()
    if not spec:
        return False
    m = _QUIET_RE.match(spec)
    if not m:
        return False
    sh, sm, eh, em = (int(x) for x in m.groups())
    if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
        return False
    cur = (now or _dt.now()).hour * 60 + (now or _dt.now()).minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end          # 跨午夜


def _normalize(text: str) -> str:
    return re.sub(r"[\s\u3000，。！？、；：,.!?;:~～\-—_]+", "", text or "")


def too_similar(reply: str, recent: list[str]) -> Optional[str]:
    """新回复和最近几条太像吗？太像就返回撞上的那条，否则 None。

    防的是"群发特征" —— 连续给不同人发几乎一样的话，是平台最容易识别为
    机器行为的形式之一。
    """
    if not recent or not reply.strip():
        return None
    a = _normalize(reply)
    if len(a) < 8:                            # 太短的（"好的""收到"）不判
        return None
    threshold = settings.similar_reply_threshold
    for prev in recent[: settings.similar_reply_window]:
        b = _normalize(prev)
        if not b:
            continue
        if a == b:
            return prev
        r = difflib.SequenceMatcher(None, a, b).ratio()
        if r >= threshold:
            return prev
    return None


def mass_send_hit(reply: str, recent_all: list[tuple[str, str]],
                  max_same: int = 3, threshold: float = 0.9) -> list[str]:
    """同一个内容短时间内发给了几个不同会话？

    这才是真正的"群发特征"：内容一样、对象不同、时间集中。
    返回撞上的会话列表（为空表示没问题）。

    注意和 too_similar 的区别：
      too_similar 比的是"同一个会话最近说过什么" → 防复读
      mass_send_hit 比的是"最近给多少人发过同样的话" → 防群发
    """
    if not reply.strip() or not recent_all:
        return []
    target = _normalize(reply)
    if len(target) < 8:
        return []
    hit: list[str] = []
    for conv, prev in recent_all:
        if conv in hit:
            continue
        p = _normalize(prev)
        if not p:
            continue
        if target == p or difflib.SequenceMatcher(None, target, p).ratio() >= threshold:
            hit.append(conv)
        if len(hit) >= max_same:
            break
    return hit


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
    paused_until: Optional[str] = None,
    sent_today: int = 0,
    recent_replies: Optional[list[str]] = None,
    recent_all: Optional[list[tuple[str, str]]] = None,
    mass_send_max_same: int = 3,
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

    # ---- 风控 1：熔断。连续失败后被暂停 ----
    if paused_until:
        return PolicyVerdict(False, f"已熔断暂停（至 {paused_until}），需人工检查")

    # ---- 风控 2：夜间静默 ----
    if in_quiet_hours():
        return PolicyVerdict(False, f"夜间静默时段（{settings.quiet_hours}），只出草稿")

    # ---- 风控 3：每日上限 ----
    if sent_today >= settings.auto_reply_max_per_day:
        return PolicyVerdict(False, f"已达每日上限（{settings.auto_reply_max_per_day} 条）")

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

    # ---- 风控 4：跨会话群发。同一内容短时间内发给多个不同会话 ----
    if recent_all:
        victims = mass_send_hit(reply, recent_all, max_same=mass_send_max_same)
        if len(victims) >= mass_send_max_same:
            return PolicyVerdict(
                False,
                f"疑似群发：同样内容最近已发给 {len(victims)} 个会话，已拦截",
            )

    # ---- 风控 4b：同会话复读。连着发几乎一样的话 ----
    if recent_replies:
        hit = too_similar(reply, recent_replies)
        if hit:
            return PolicyVerdict(
                False,
                f"与最近发过的内容过于相似（「{hit[:20]}…」），疑似群发特征",
            )

    if auto_sent_last_minute >= settings.auto_reply_max_per_minute:
        return PolicyVerdict(False, "触发频率上限")

    last = _parse_iso(last_auto_sent_at)
    if last is not None:
        delta = (datetime.now(timezone.utc) - last).total_seconds()
        if delta < settings.min_reply_interval_seconds:
            return PolicyVerdict(False, f"距上次自动回复仅 {delta:.1f}s，低于最小间隔")

    return PolicyVerdict(True, "允许自动发送")
