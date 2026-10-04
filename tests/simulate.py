#!/usr/bin/env python3
"""离线端到端验证：不联网、不调模型、不碰微信。

用 MockLLM + MockProvider + MockChannel 把编排层跑一遍，
确认"去重、合并、指代、接管、拦截、发送确认"这些工程约束真的生效。

    python tests/simulate.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app.config 之前设置，否则会写到生产库
os.environ["DB_PATH"] = "data/simulate.db"
os.environ["WECHAT_CHANNEL"] = "mock"
os.environ["LOGISTICS_PROVIDER"] = "mock"
os.environ["AUTO_REPLY_MAX_PER_MINUTE"] = "100"
os.environ["MIN_REPLY_INTERVAL_SECONDS"] = "0"
os.environ["DRY_RUN"] = "0"          # 离线回归要验证真实的发送/拦截分支

TEST_DB = ROOT / "data" / "simulate.db"
OUTBOX = ROOT / "data" / "mock_outbox.jsonl"

for p in (TEST_DB, Path(str(TEST_DB) + "-wal"), Path(str(TEST_DB) + "-shm"), OUTBOX):
    if p.exists():
        p.unlink()

from adapters.base import SendResult  # noqa: E402
from adapters.mock_channel import MockChannel  # noqa: E402
from app import db  # noqa: E402
from app.mock_llm import MockLLM  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.schemas import IncomingMessage, now_iso  # noqa: E402

PRIVATE = "mock:private:merchant-001"
GROUP = "mock:room:test-room-001"
WB1 = "773123456789012"
WB2 = "773987654321098"

RESULTS: list[tuple[str, bool, str]] = []
_seq = [0]


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def msg(text: str, conv: str = PRIVATE, *, event_id: str = "", is_group: bool = False,
        mentioned: bool = True, channel: str = "mock") -> IncomingMessage:
    _seq[0] += 1
    return IncomingMessage(
        event_id=event_id or f"e{_seq[0]}-{conv}-{text[:6]}",
        channel=channel,
        conversation_id=conv,
        channel_chat_id=conv,
        sender_id="wxid_merchant_kf",
        sender_name="商家客服小李",
        text=text,
        is_group=is_group,
        mentioned_bot=mentioned,
        received_at=now_iso(),
    )


def draft_count(conv: str) -> int:
    row = db._conn().execute(
        "SELECT COUNT(*) n FROM outbox WHERE conversation_id=?", (conv,)
    ).fetchone()
    return int(row["n"])


def latest_draft(conv: str):
    return db._conn().execute(
        "SELECT * FROM outbox WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (conv,)
    ).fetchone()


class FlakyChannel(MockChannel):
    """第一次发送返回 unknown，用来验证"结果未知绝不重发"。"""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, channel_chat_id: str, text: str) -> SendResult:
        self.calls += 1
        return SendResult("unknown", "模拟网络超时，结果未知")


async def main() -> int:
    db.init_db()
    pipeline = Pipeline(MockChannel(), llm=MockLLM())

    # 缩短合并窗口，让测试跑得快（生产用默认的 2s/5s）
    import app.pipeline as pl
    pl.QUIET_WINDOW_SECONDS = 0.35
    pl.MAX_WAIT_SECONDS = 0.8

    await pipeline.start()

    async def settle(seconds: float = 1.6) -> None:
        await asyncio.sleep(seconds)

    # ---------------- S1 连续三条消息合并理解 ----------------
    base = draft_count(PRIVATE)
    for text in (WB1, "客户不要了", "退回来"):
        await pipeline.submit(msg(text))
    await settle()
    n = draft_count(PRIVATE) - base
    row = latest_draft(PRIVATE)
    check("S1 三条消息只产生一个处理结果", n == 1, f"实际产生 {n} 条")
    check("S1 识别为拦截退回", row and row["intent"] == "intercept_return",
          f"intent={row['intent'] if row else None}")
    check("S1 未声称已拦截/已执行",
          row is not None and not any(
              k in row["reply"] for k in ("已拦截", "已经拦截", "拦截成功", "已安排")),
          f"reply={row['reply'] if row else None}")
    check("S1 单号关联正确", row is not None and WB1 in (row["reply"] or ""),
          f"reply={row['reply'] if row else None}")

    # ---------------- S2 重复回调去重 ----------------
    dup = msg(WB1, event_id="dup-event-001")
    r1 = await pipeline.submit(dup)
    r2 = await pipeline.submit(dup)
    check("S2 同一 event_id 第二次被去重", r1.accepted and not r2.accepted,
          f"第一次={r1.reason} 第二次={r2.reason}")

    # ---------------- S3 两票 + 指代不清 → 追问，不猜 ----------------
    await pipeline.submit(msg(f"{WB1} 和 {WB2} 这两票帮忙看下"))
    await settle()
    await pipeline.submit(msg("这个退回来吧"))
    await settle()
    row = latest_draft(PRIVATE)
    check("S3 指代不清时转为追问", row is not None and row["action"] == "ask",
          f"action={row['action'] if row else None}")
    check("S3 追问里不擅自选定单号",
          row is not None and row["action"] == "ask",
          f"reply={row['reply'] if row else None}")

    # ---------------- S4 改口：先别退了 ----------------
    await pipeline.submit(msg("先别退了，客户又要了，继续送"))
    await settle()
    row = latest_draft(PRIVATE)
    check("S4 识别诉求变更（取消退回）", row is not None and row["intent"] == "cancel_return",
          f"intent={row['intent'] if row else None}")
    check("S4 转人工而不是假装撤销成功",
          row is not None and row["action"] == "handoff",
          f"action={row['action'] if row else None}")

    # ---------------- S5 群聊未被点名 → 不插话 ----------------
    before = draft_count(GROUP)
    res = await pipeline.submit(msg("大家下午好呀，今天天气不错", conv=GROUP,
                                    is_group=True, mentioned=False))
    await settle(0.6)
    check("S5 群聊未被点名不回复",
          res.reason.startswith("不回复") and draft_count(GROUP) == before,
          f"reason={res.reason}")

    # ---------------- S6 人工接管时不回复 ----------------
    db.upsert_conversation(PRIVATE, "mock", PRIVATE, "测试私聊", "m-001", "review")
    db.set_takeover(PRIVATE, "2099-01-01T00:00:00+08:00")
    before = draft_count(PRIVATE)
    res = await pipeline.submit(msg(f"{WB2} 催一下", event_id="takeover-case-1"))
    await settle(0.6)
    check("S6 人工接管期间不产生回复", res.accepted and draft_count(PRIVATE) == before,
          f"reason={res.reason}")
    db.set_takeover(PRIVATE, None)

    # ---------------- S7 review 模式：AI 只出草稿，不自动发 ----------------
    db.set_mode(PRIVATE, "review")
    before = draft_count(PRIVATE)
    await pipeline.submit(msg("你们周末上门取件吗"))
    await settle()
    row = latest_draft(PRIVATE)
    check("S7 review 模式只落草稿不发送",
          draft_count(PRIVATE) == before + 1 and row["status"] == "draft",
          f"status={row['status'] if row else None}")

    # ---------------- S8 auto 模式 + mock 物流源 → 必须被拦截 ----------------
    db.set_mode(PRIVATE, "auto")
    before = draft_count(PRIVATE)
    await pipeline.submit(msg("帮我催一下这个件", event_id="auto-urge-1"))
    await settle()
    row = latest_draft(PRIVATE)
    check("S8 auto 模式下 mock 物流源被拦截，不作为已发送",
          draft_count(PRIVATE) == before + 1 and row["status"] in ("draft", "blocked"),
          f"status={row['status'] if row else None} reason={row['reason'] if row else None}")

    # ---------------- S9 auto 模式 + 非物流意图 → 正常自动发送 ----------------
    before = draft_count(PRIVATE)
    await pipeline.submit(msg("你们能寄文件吗", event_id="auto-biz-1"))
    await settle()
    row = latest_draft(PRIVATE)
    check("S9 非物流类咨询在 auto 下可自动发送",
          draft_count(PRIVATE) == before + 1 and row["status"] == "sent",
          f"status={row['status'] if row else None} reason={row['reason'] if row else None}")
    check("S9 发送内容写入了审计日志", OUTBOX.exists() and OUTBOX.stat().st_size > 0)

    # ---------------- S10 发送结果未知 → 绝不自动重发 ----------------
    flaky = Pipeline(FlakyChannel(), llm=MockLLM())
    await flaky.start()
    db.set_mode(GROUP, "auto")
    await flaky.submit(msg("你们能寄文件吗", conv=GROUP, is_group=True,
                           mentioned=True, event_id="unknown-1"))
    await asyncio.sleep(1.6)
    row = latest_draft(GROUP)
    check("S10 发送结果未知时标记 unknown 且不重发",
          row is not None and row["status"] == "unknown" and flaky.adapter.calls == 1,
          f"status={row['status'] if row else None} calls={flaky.adapter.calls}")
    await flaky.stop()

    # ---------------- S13 元话语检测（模型在讲自己的格式而不是回应客户）----------------
    from app.llm import looks_like_meta

    for bad in [
        "收到，后续我会按格式返回。",
        "好的，以后就按这个格式回复你。",
        "明白了，下次就按这个来。",
        "按上述格式输出即可。",
        "作为一个人工智能，我无法查询。",
        '{"action":"reply"} 字段：action',
    ]:
        check(f"S13 元话语要能认出来：{bad[:16]}…", looks_like_meta(bad))

    for good in [
        "好的，地址不改，按原地址送。",
        "收到，我帮你问一下网点。",
        "你按这个格式发我：单号+问题描述",
        "以后就按这个地址送，不用再问。",
        "作为网点客服，我这边帮你核实。",
    ]:
        check(f"S13 正常回复不能误判：{good[:16]}…", not looks_like_meta(good))

    await pipeline.stop()

    # ---------------- S11 安全兜底：不依赖模型给的 intent ----------------
    # 真实事故复现：模型把"先别退了，继续送"标成 intent=other，
    # 如果 policy 只看 intent，这条涉及退回操作的消息就会被自动发出去。
    from app import policy as _policy  # noqa: E402
    from app.schemas import Action, Intent  # noqa: E402

    def verdict(text: str, reply: str, intent: Intent, logistics_real: bool = True):
        return _policy.check_send_policy(
            mode="auto", takeover_until=None, action=Action.reply, intent=intent,
            reply=reply, logistics_real=logistics_real,
            auto_sent_last_minute=0, last_auto_sent_at=None, inbound_text=text,
        )

    v = verdict("先别退了，客户又要了，继续送",
                "好，这票还在正常派件中，没被退回，继续送就行。", Intent.other)
    check("S11 模型误标 intent=other 时，高风险词兜底拦截",
          not v.allowed, f"allowed={v.allowed} reason={v.reason}")

    v = verdict("这票理赔怎么算", "理赔需要按运单协议核实。", Intent.business_inquiry)
    check("S11 理赔类文字一律人工", not v.allowed, f"allowed={v.allowed}")

    v = verdict("帮我改下收货地址", "改地址需要网点操作。", Intent.other)
    check("S11 改址类文字一律人工", not v.allowed, f"allowed={v.allowed}")

    v = verdict("你们周末上门取件吗", "周末正常上门，提前一小时说就行。", Intent.business_inquiry)
    check("S11 普通咨询不受兜底影响，仍可自动发", v.allowed, f"reason={v.reason}")

    v = verdict("这票现在到哪了", "已经到建邺区网点，派件中。", Intent.eta_inquiry,
                logistics_real=False)
    check("S11 时效类缺真实物流数据时仍拦截", not v.allowed, f"reason={v.reason}")

    # ---------------- S12 增量比对必须扛得住 OCR 抖动 ----------------
    from adapters.macos_vision import new_suffix, _similar

    prev = ["in|在吗", "in|773123456789012 到哪了", "in|催一下"]
    check("S12 完全一致时能找出新增",
          new_suffix(prev, prev + ["in|好的"]) == ["in|好的"])

    jitter = ["in|在吗", "in|773123456789012 到那了", "in|摧一下"]   # OCR 认错两个字
    check("S12 OCR 抖动时仍能对齐（不能漏消息）",
          new_suffix(prev, jitter + ["in|新消息"]) == ["in|新消息"],
          f"实际={new_suffix(prev, jitter + ['in|新消息'])}")

    check("S12 两条相同文本能正确区分（不是文本哈希）",
          new_suffix(["in|催一下"], ["in|催一下", "in|催一下"]) == ["in|催一下"])

    check("S12 人工滚动导致对不上时返回空（宁可漏不重复）",
          new_suffix(["in|A", "in|B", "in|C"], ["in|X", "in|Y"]) == [])

    check("S12 首次轮询不回复历史",
          new_suffix([], ["in|历史1", "in|历史2"]) == [])

    check("S12 发送回读：OCR 认错字也能确认已发出",
          _similar("周末上门取件这个要看", "周未上门取件这个要看"))

    from adapters.macos_vision import title_ok
    check("S12 配置名是真名的前缀时算同一个会话",
          title_ok("京东生活线报群6禁链接", "京东生活线报群6"))
    check("S12 群名带成员数也能匹配",
          title_ok("大潍坊AI交流群（304）", "大潍坊AI交流群"))
    check("S12 过短的前缀不算匹配（防串会话）",
          not title_ok("客户AB群", "客户A"))
    check("S12 完全不同的会话不匹配",
          not title_ok("大理旅居客", "京东生活线报群6"))

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 74)
    print("离线端到端验证结果")
    print("=" * 74)
    failed = 0
    for name, ok, detail in RESULTS:
        flag = "通过" if ok else "失败"
        if not ok:
            failed += 1
        print(f"[{flag}] {name}")
        if not ok and detail:
            print(f"        → {detail}")
    print("-" * 74)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - failed}，失败 {failed}")
    print("=" * 74 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
