"""编排核心：去重 → 合并连续消息 → 调模型 → 发送前复核 → 落库。

这里承担所有"什么时候能发、发给谁"的判断。模型只负责理解和组织语言。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from . import db, policy
from .config import load_knowledge, settings
from .llm import CustomerServiceLLM, LLMOutcome, build_llm
from .prompts import SYSTEM_PROMPT, build_context_block
from .schemas import Action, ConversationMode, IncomingMessage, now_iso
from .tools import ToolRegistry

log = logging.getLogger("pipeline")

QUIET_WINDOW_SECONDS = 2.0     # 连续消息合并窗口
MAX_WAIT_SECONDS = 5.0         # 一个批次最多等多久


@dataclass
class SubmitResult:
    accepted: bool
    reason: str
    conversation_id: str = ""
    queue_depth: int = 0


class ConvBuffer:
    """每个会话一个缓冲区。支持处理失败时把整批消息放回队首重来。"""

    def __init__(self) -> None:
        self.items: deque[tuple[IncomingMessage, int]] = deque()
        self.event = asyncio.Event()

    def push(self, item: tuple[IncomingMessage, int]) -> None:
        self.items.append(item)
        self.event.set()

    def push_front(self, items: list[tuple[IncomingMessage, int]]) -> None:
        self.items.extendleft(reversed(items))
        self.event.set()

    def __len__(self) -> int:
        return len(self.items)

    async def _wait_nonempty(self) -> None:
        while not self.items:
            self.event.clear()
            await self.event.wait()

    async def get_batch(self, quiet: float, max_wait: float) -> list[tuple[IncomingMessage, int]]:
        await self._wait_nonempty()
        batch = [self.items.popleft()]
        deadline = time.monotonic() + max_wait
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            slot = min(quiet, remain)
            if not self.items:
                self.event.clear()
                if not self.items:
                    try:
                        await asyncio.wait_for(self.event.wait(), slot)
                    except asyncio.TimeoutError:
                        pass
            if not self.items:
                break
            batch.append(self.items.popleft())
        return batch


class Pipeline:
    def __init__(self, adapter: Any, llm: Optional[Any] = None) -> None:
        self.adapter = adapter
        self.llm = llm or build_llm()
        self.buffers: dict[str, ConvBuffer] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self._running = False
        self.stats = {"received": 0, "composed": 0, "deduped": 0, "ignored": 0,
                      "drafts": 0, "sent": 0, "blocked": 0, "unknown": 0, "failed": 0, "paused": 0,
            "media": 0, "media_ok": 0, "media_failed": 0}

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self._running = True
        # 有些通道是"主动看屏幕/轮询"型（macOS 截屏、PC Hook），没有推送回调
        if hasattr(self.adapter, "poll"):
            self.workers["__poll__"] = asyncio.create_task(
                self._poll_loop(), name="poll-loop"
            )

    async def stop(self) -> None:
        self._running = False
        for task in self.workers.values():
            task.cancel()
        self.workers.clear()

    # ---------------- 入站 ----------------
    async def submit(self, msg: IncomingMessage) -> SubmitResult:
        self.stats["received"] += 1

        # 非文字消息（图片/视频）：先送视觉模型看懂，再当成普通消息往下走。
        # 放在这里是因为 submit 是异步的，而视觉调用要几秒；
        # 图片内容分析完之后 msg.text 就是一段描述，后面所有逻辑都不用改。
        if msg.media and msg.media_path:
            await self._understand_media(msg)

        rule = policy.conversation_rule(msg.channel, msg.channel_chat_id)
        if rule is None:
            return SubmitResult(False, "会话不在白名单，未接入")

        db.upsert_conversation(
            conv_id=msg.conversation_id,
            channel=msg.channel,
            channel_chat_id=msg.channel_chat_id,
            title=str(rule.get("title", "")),
            merchant_id=str(rule.get("merchant_id", "")),
            mode=str(rule.get("mode", ConversationMode.review.value)),
        )

        row_id = db.save_incoming(msg)
        if row_id is None:
            self.stats["deduped"] += 1
            return SubmitResult(False, "重复消息，已忽略", msg.conversation_id)

        db.bump_version(msg.conversation_id)

        if db.is_human_taken_over(msg.conversation_id):
            self.stats["ignored"] += 1
            return SubmitResult(True, "人工接管中，仅记录不回复", msg.conversation_id)

        verdict = policy.should_respond(
            msg, bool(rule.get("require_mention_in_group", True))
        )
        if not verdict.allowed:
            self.stats["ignored"] += 1
            return SubmitResult(True, f"不回复：{verdict.reason}", msg.conversation_id)

        buf = self.buffers.setdefault(msg.conversation_id, ConvBuffer())
        buf.push((msg, row_id))
        self._ensure_worker(msg.conversation_id)
        return SubmitResult(True, "已入队", msg.conversation_id, len(buf))

    def _ensure_worker(self, conv_id: str) -> None:
        task = self.workers.get(conv_id)
        if task is None or task.done():
            self.workers[conv_id] = asyncio.create_task(
                self._worker(conv_id), name=f"worker-{conv_id}"
            )

    async def _poll_loop(self) -> None:
        """主动轮询型通道的收消息循环。"""
        while self._running:
            try:
                # poll() 是阻塞的（截屏 + OCR），扔到线程里别卡住事件循环
                msgs = await asyncio.to_thread(self.adapter.poll)
                for msg in msgs:
                    res = await self.submit(msg)
                    log.info("轮询到消息 %s → %s", msg.event_id, res.reason)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("轮询失败")
            await asyncio.sleep(settings.poll_interval)

    async def _worker(self, conv_id: str) -> None:
        buf = self.buffers[conv_id]
        while self._running:
            try:
                batch = await asyncio.wait_for(
                    buf.get_batch(QUIET_WINDOW_SECONDS, MAX_WAIT_SECONDS), timeout=1800
                )
            except asyncio.TimeoutError:
                return                      # 长时间空闲，回收 worker
            except asyncio.CancelledError:
                return
            try:
                await self._process(conv_id, batch, buf)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("处理会话 %s 失败", conv_id)

    # ---------------- 处理 ----------------
    async def _think(
        self,
        conv_id: str,
        batch_text: str,
        history: list[dict[str, str]],
        row: Any,
        is_group: bool,
        batch_id: str,
    ) -> tuple[LLMOutcome, list[dict[str, Any]], str]:
        """跑一次模型决策。返回 (outcome, evidence, mock_flag)。"""
        cases = [f"{c['waybill_no'] or '(无单号)'} · {c['intent']} · {c['summary']}"
                 for c in db.open_cases(conv_id)]
        context_block = build_context_block(
            title=row["title"],
            is_group=is_group,
            merchant=row["merchant_id"],
            knowledge=load_knowledge(),
            open_cases=cases,
        )

        registry = ToolRegistry(conv_id)
        outcome: LLMOutcome = await self.llm.decide(
            conversation_id=conv_id,
            batch_id=batch_id,
            history=history,
            batch_text=batch_text,
            context_block=context_block,
            system_prompt=SYSTEM_PROMPT,
            tools=registry,
        )

        db.record_model_call(
            conv_id, batch_id, outcome.model, outcome.ok, outcome.tool_rounds,
            outcome.latency_ms, error=outcome.error,
        )

        evidence = [e.model_dump() for e in outcome.evidence]

        # 用了模拟数据源的草稿必须显眼提示审核人，避免"看着像真轨迹"被直接点发送
        used_mock = any(
            e.tool == "query_logistics" and e.summary.lower().startswith("mock")
            for e in outcome.evidence
        )
        mock_flag = "⚠ 本草稿引用了模拟物流数据，禁止对外发送；" if used_mock else ""
        return outcome, evidence, mock_flag

    async def _process(self, conv_id: str, batch: list[tuple[IncomingMessage, int]],
                       buf: ConvBuffer) -> None:
        if db.is_human_taken_over(conv_id):
            return

        row = db.get_conversation(conv_id)
        if row is None:
            return
        version_at_start = int(row["version"])
        batch_id = uuid.uuid4().hex[:12]

        batch_text = "\n".join(m.text for m, _ in batch)
        min_id = min(i for _, i in batch)
        history = db.history_before(conv_id, min_id, limit=20)

        outcome, evidence, mock_flag = await self._think(
            conv_id, batch_text, history, row, bool(batch[0][0].is_group), batch_id
        )

        decision = outcome.decision
        if decision.action == Action.wait:
            return

        allowed = policy.check_send_policy(
            mode=row["mode"],
            takeover_until=row["takeover_until"],
            action=decision.action,
            intent=decision.intent,
            reply=decision.reply,
            logistics_real=outcome.logistics_real,
            auto_sent_last_minute=db.auto_sent_count_last_minute(conv_id),
            last_auto_sent_at=db.last_auto_sent_at(conv_id),
            inbound_text=batch_text,
            # 风控参数
            paused_until=db.paused_until(conv_id),
            sent_today=db.sent_count_today(conv_id),
            recent_replies=db.recent_sent_replies(conv_id),
            recent_all=db.recent_sent_all(),
            mass_send_max_same=3,
        )

        if not allowed.allowed or mock_flag:
            db.create_draft(
                conv_id, batch_id, decision.action.value, decision.intent.value,
                decision.reply, evidence, status="draft",
                reason=f"{mock_flag}{allowed.reason if not allowed.allowed else '引用了模拟数据源'}｜{decision.handoff_reason}",
            )
            self.stats["drafts"] += 1
            return

        draft_id = db.create_draft(
            conv_id, batch_id, decision.action.value, decision.intent.value,
            decision.reply, evidence, status="approved", reason=decision.handoff_reason,
        )
        await self._deliver(
            draft_id=draft_id,
            conv_id=conv_id,
            channel_chat_id=row["channel_chat_id"],
            text=decision.reply,
            expect_version=version_at_start,
            manual=False,
        )

    # ---------------- 半自动：人工粘消息，AI 出草稿 ----------------
    async def compose(
        self,
        *,
        conversation_id: str,
        text: str,
        title: str = "",
        sender_name: str = "",
        is_group: bool = False,
    ) -> dict[str, Any]:
        """人工把商家消息粘进来，AI 出草稿，人工自己复制到微信里发。

        与自动通道的三点区别：
        1. 不走合并窗口 —— 人工在等，立刻处理
        2. 不看群聊点名规则 —— 人工既然粘进来了，就是要处理它
        3. 永远只出草稿，绝不自动发送 —— 发送动作本来就在人手里
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "内容为空"}

        conversation_id = (conversation_id or "").strip()
        if not conversation_id:
            return {"ok": False, "error": "请先选择或新建会话"}

        row = db.get_conversation(conversation_id)
        if row is None:
            db.upsert_conversation(
                conv_id=conversation_id,
                channel="manual",
                channel_chat_id=conversation_id,
                title=title or conversation_id,
                merchant_id="",
                mode=ConversationMode.review.value,
            )
            row = db.get_conversation(conversation_id)
        elif title and title != row["title"]:
            db.set_conversation_title(conversation_id, title)
            row = db.get_conversation(conversation_id)
        if row is None:
            return {"ok": False, "error": "会话创建失败"}

        msg = IncomingMessage(
            event_id=f"manual-{uuid.uuid4().hex}",
            channel=row["channel"],
            conversation_id=conversation_id,
            channel_chat_id=row["channel_chat_id"],
            sender_id="manual-input",
            sender_name=sender_name or "商家客服",
            text=text,
            is_group=is_group,
            mentioned_bot=True,
        )
        msg_id = db.save_incoming(msg)
        db.bump_version(conversation_id)
        batch_id = uuid.uuid4().hex[:12]
        history = db.history_before(conversation_id, msg_id or 0, limit=20)

        outcome, evidence, mock_flag = await self._think(
            conversation_id, text, history, row, is_group, batch_id
        )
        decision = outcome.decision

        reply = decision.reply
        if decision.action == Action.wait and not reply.strip():
            reply = "（模型判断这条不需要回复，可能是群里的闲聊）"

        draft_id = db.create_draft(
            conversation_id, batch_id, decision.action.value, decision.intent.value,
            reply, evidence, status="draft",
            reason=f"{mock_flag}半自动模式：人工录入，AI 草稿待复制发送｜{decision.handoff_reason}",
        )
        self.stats["drafts"] += 1
        self.stats["composed"] += 1

        return {
            "ok": True,
            "draft_id": draft_id,
            "conversation_id": conversation_id,
            "action": decision.action.value,
            "intent": decision.intent.value,
            "reply": reply,
            "handoff_reason": decision.handoff_reason,
            "waybill_numbers": decision.waybill_numbers,
            "logistics_real": outcome.logistics_real,
            "mock_warning": bool(mock_flag),
            "model_ok": outcome.ok,
            "evidence": evidence,
        }

    # ---------------- 出站 ----------------
    async def _deliver(
        self,
        *,
        draft_id: int,
        conv_id: str,
        channel_chat_id: str,
        text: str,
        expect_version: Optional[int],
        manual: bool,
    ) -> str:
        text = (text or "").strip()
        if not text:
            db.update_draft(draft_id, status="discarded", reason="内容为空")
            return "discarded"

        row = db.get_conversation(conv_id)
        if row is None:
            db.update_draft(draft_id, status="failed", send_result="会话不存在")
            return "failed"

        if db.is_human_taken_over(conv_id) and not manual:
            db.update_draft(draft_id, status="blocked", reason="发送前发现人工已接管")
            self.stats["blocked"] += 1
            return "blocked"

        if settings.dry_run and not manual:
            db.update_draft(draft_id, status="draft",
                            reason="演练模式（DRY_RUN）不发送，仅出草稿")
            self.stats["drafts"] += 1
            return "draft"

        # ---- 风控 5：随机延迟。固定节奏是典型的机器特征 ----
        if not manual:
            delay = random.uniform(settings.reply_delay_min, settings.reply_delay_max)
            log.debug("随机延迟 %.1fs 后发送", delay)
            await asyncio.sleep(delay)

        if expect_version is not None and int(row["version"]) != expect_version:
            # 思考期间（含随机延迟期间）对方又发了新要求，这条草稿已经过期
            db.update_draft(draft_id, status="discarded", reason="上下文已变化，草稿作废")
            return "discarded"

        db.update_draft(draft_id, status="sending")

        # 半自动通道没有适配器可发 —— 人已经把内容复制到微信里发出去了，
        # 这里只负责记账（写入历史 + 标记已发送），不碰微信。
        if row["channel"] == "manual":
            db.update_draft(draft_id, status="sent",
                            send_result="人工复制发送（半自动模式）", sent_at=now_iso())
            db.save_outgoing(conv_id, text)
            self.stats["sent"] += 1
            return "sent"

        try:
            if not await self.adapter.verify_target(channel_chat_id):
                db.update_draft(draft_id, status="blocked",
                                send_result="目标校验失败，已拒绝发送")
                self.stats["blocked"] += 1
                return "blocked"
            result = await self.adapter.send(channel_chat_id, text)
        except Exception as exc:
            db.update_draft(draft_id, status="unknown",
                            send_result=f"发送异常，结果未知：{type(exc).__name__}")
            self.stats["unknown"] += 1
            return "unknown"

        if result.status == "sent":
            db.update_draft(draft_id, status="sent", send_result=result.detail,
                            sent_at=now_iso())
            db.save_outgoing(conv_id, text)
            if not manual:
                db.mark_auto_sent(conv_id)
            self.stats["sent"] += 1
            db.record_send_outcome(conv_id, True)
        elif result.status == "unknown":
            # 结果未知绝不重发，交人工判断
            db.update_draft(draft_id, status="unknown", send_result=result.detail)
            self.stats["unknown"] += 1
            self._note_failure(conv_id, "发送结果未知")
        else:
            db.update_draft(draft_id, status="failed", send_result=result.detail)
            self.stats["failed"] += 1
            self._note_failure(conv_id, f"发送失败：{result.detail[:60]}")
        return result.status

    # ---------------- 非文字消息理解 ----------------
    async def _understand_media(self, msg: IncomingMessage) -> None:
        """把图片/视频封面送视觉模型，用得到的描述替换消息文本。"""
        from .media import describe_media

        try:
            res = await describe_media(
                msg.media_path, (0, 0, 0, 0),   # 已经是裁好的图，不需要再裁
                kind=msg.media, conversation=msg.conversation_id,
            )
        except Exception:
            log.exception("媒体理解异常")
            msg.text = f"[商家发来一张{'视频' if msg.media == 'video' else '图片'}]（识别失败）"
            return

        self.stats["media"] = self.stats.get("media", 0) + 1
        if res.ok:
            msg.text = res.text
            self.stats["media_ok"] = self.stats.get("media_ok", 0) + 1
            log.info("媒体理解完成（%s，%.1fs，缓存=%s，%d 字）",
                     msg.media, res.seconds, res.cached, len(res.text))
        else:
            # 看不懂也要让 AI 知道"来了张图，但没看清"，否则它会以为没收到东西
            msg.text = (f"[商家发来一张{'视频' if msg.media == 'video' else '图片'}，"
                        f"但系统没能识别出内容]")
            self.stats["media_failed"] = self.stats.get("media_failed", 0) + 1
            log.warning("媒体理解失败：%s", res.error)

    # ---------------- 风控 6：熔断 ----------------
    def _note_failure(self, conv_id: str, reason: str) -> None:
        """连续失败到阈值就暂停这个会话，避免一直撞墙。"""
        n = db.record_send_outcome(conv_id, False)
        limit = settings.circuit_breaker_failures
        if n >= limit:
            until = db.pause_conversation(
                conv_id, settings.circuit_breaker_cooldown_minutes,
                f"连续 {n} 次失败：{reason}",
            )
            self.stats["paused"] = self.stats.get("paused", 0) + 1
            log.error("会话 %s 连续 %d 次失败，已熔断暂停至 %s（%s）",
                      conv_id, n, until, reason)

    async def deliver_manual(self, draft_id: int, text: Optional[str] = None) -> str:
        """人工审核台点"发送"。人工操作不再受自动发送策略限制。"""
        draft = db.get_draft(draft_id)
        if draft is None:
            return "not_found"
        if draft["status"] == "sent":
            return "already_sent"
        conv = db.get_conversation(draft["conversation_id"])
        if conv is None:
            return "not_found"
        if text is not None:
            db.update_draft(draft_id, reply=text)
        return await self._deliver(
            draft_id=draft_id,
            conv_id=draft["conversation_id"],
            channel_chat_id=conv["channel_chat_id"],
            text=text if text is not None else draft["reply"],
            expect_version=None,
            manual=True,
        )


_pipeline: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        from adapters import get_adapter

        llm = None
        if settings.llm.backend == "mock":
            from .mock_llm import MockLLM

            llm = MockLLM()
            log.warning("LLM_BACKEND=mock：使用离线假模型，仅供联调，不可用于生产")
        _pipeline = Pipeline(get_adapter(), llm=llm)
    return _pipeline


def set_pipeline(p: Pipeline) -> None:
    global _pipeline
    _pipeline = p
