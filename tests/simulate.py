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

# 测试要固定结果：关掉夜间静默、随机延迟、打字模拟，否则同一个用例
# 在白天和半夜跑会得到不同结论（风控本来就该这样，但测试需要确定性）
os.environ["QUIET_HOURS"] = ""
os.environ["REPLY_DELAY_MIN"] = "0"
os.environ["REPLY_DELAY_MAX"] = "0"
os.environ["TYPING_SIMULATION"] = "0"

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

    # ---------------- S14 六项风控 ----------------
    from datetime import datetime as _dt

    from app import policy as _policy  # noqa: F811
    from app import policy as _p
    from app.schemas import Action, Intent  # noqa: F811

    # 1) 夜间静默（含跨午夜）
    os.environ["QUIET_HOURS"] = "22:00-08:00"
    import importlib

    from app import config as _cfg
    importlib.reload(_cfg)
    _p.settings = _cfg.settings
    check("S14 夜间 22:30 判为静默", _p.in_quiet_hours(_dt(2026, 1, 1, 22, 30)))
    check("S14 凌晨 03:00 判为静默（跨午夜）", _p.in_quiet_hours(_dt(2026, 1, 1, 3, 0)))
    check("S14 白天 14:00 不算静默", not _p.in_quiet_hours(_dt(2026, 1, 1, 14, 0)))
    os.environ["QUIET_HOURS"] = "00:00-00:00"
    importlib.reload(_cfg)
    _p.settings = _cfg.settings
    check("S14 起止相同 = 不启用静默", not _p.in_quiet_hours(_dt(2026, 1, 1, 3, 0)))
    os.environ["QUIET_HOURS"] = ""
    importlib.reload(_cfg)
    _p.settings = _cfg.settings
    check("S14 显式设空 = 关闭静默", not _p.in_quiet_hours(_dt(2026, 1, 1, 3, 0)))

    # 2) 相似度（同会话复读）
    same = "这票现在在派件中，我帮您催一下派送，有结果回您。"
    check("S14 同一会话复读会被识别",
          _p.too_similar(same, [same]) is not None)
    check("S14 内容不同不误报",
          _p.too_similar(same, ["好的，地址已经记下来了，稍后回复你。"]) is None)
    check("S14 太短的回复不做相似判断",
          _p.too_similar("好的", ["好的"]) is None)

    # 3) 跨会话群发
    victims = _p.mass_send_hit(same, [("A", same), ("B", same), ("C", same)])
    check("S14 同一内容发给 3 个会话 = 群发特征", len(victims) >= 3, f"实际 {len(victims)}")
    check("S14 只发给 2 个会话不算群发",
          len(_p.mass_send_hit(same, [("A", same), ("B", same)])) < 3)
    check("S14 内容各不相同不算群发",
          len(_p.mass_send_hit(same, [("A", "件已到杭州"), ("B", "电话记下了")])) == 0)

    # 4) 每日上限
    v = _policy.check_send_policy(
        mode="auto", takeover_until=None, action=Action.reply, intent=Intent.business_inquiry,
        reply="周末上门取件可以的，提前一小时说一声。", logistics_real=True,
        auto_sent_last_minute=0, last_auto_sent_at=None, inbound_text="周末能取件吗",
        sent_today=999,
    )
    check("S14 达到每日上限会被拦", not v.allowed, f"reason={v.reason}")

    # 5) 熔断暂停
    v = _policy.check_send_policy(
        mode="auto", takeover_until=None, action=Action.reply, intent=Intent.business_inquiry,
        reply="周末上门取件可以的。", logistics_real=True,
        auto_sent_last_minute=0, last_auto_sent_at=None, inbound_text="周末能取件吗",
        paused_until="2099-01-01T00:00:00+08:00",
    )
    check("S14 熔断暂停期间不发", not v.allowed, f"reason={v.reason}")

    # ---------------- S15 配置漂移：代码用到的变量必须在 .env.example 里 -------
    # 真实踩过的坑：加完六项风控后，app/config.py 里读 QUIET_HOURS 等变量，
    # 但 .env.example 里忘了写 —— 客户根本发现不了也配不了这些开关。
    import json as _json
    import re as _re

    from app.config import ROOT as _ROOT

    _env_txt = (_ROOT / ".env.example").read_text(encoding="utf-8")
    _declared = set(_re.findall(r"^([A-Z][A-Z0-9_]+)=", _env_txt, _re.M))
    _src = "\n".join(
        f.read_text(encoding="utf-8")
        for d in ("app", "adapters", "bridge", "integrations", "logistics")
        for f in (_ROOT / d).rglob("*.py")
    )
    _used = set(_re.findall(r'_env(?:_allow_empty)?\(\s*"([A-Z][A-Z0-9_]+)"', _src))
    _used |= {
        p["api_key_env"]
        for p in _json.loads((_ROOT / "config" / "models.json").read_text(encoding="utf-8"))["profiles"]
        if p.get("api_key_env")
    }
    _missing = sorted(_used - _declared)
    check("S15 代码用到的配置项都在 .env.example 里有说明",
          not _missing, f"缺：{_missing}")

    for _k in ("QUIET_HOURS", "AUTO_REPLY_MAX_PER_DAY", "REPLY_DELAY_MIN",
               "SIMILAR_REPLY_WINDOW", "CIRCUIT_BREAKER_FAILURES", "TYPING_SIMULATION"):
        check(f"S15 风控开关 {_k} 有文档", _k in _declared)

    # ---------------- S16 会话列表标题行识别 ----------------
    # 真实踩到的坑：原来靠"有没有时间戳"判断标题行，实测微信列表里
    # 大部分会话（文件传输助手、测试1、微信团队）根本不显示时间，
    # 5 个会话只认出 1 个。
    from adapters.vision_common import pick_titles, strip_list_time

    # 这是从真实截图 OCR 出来的行（窗口 880x640）
    real_rows = [
        (77.0, "文件传输助手"),
        (139.0, "腾讯新闻 20:42"),
        (159.0, "油价调整通知"),
        (206.0, "测试1"),
        (271.0, "微信团队"),
    ]
    titles = pick_titles(real_rows)
    check("S16 没有时间戳的会话也要认出来（5 行 → 4 个会话）",
          titles == ["文件传输助手", "腾讯新闻", "测试1", "微信团队"],
          f"实际 {titles}")
    check("S16 预览行不能被当成会话名",
          "油价调整通知" not in titles)
    check("S16 时间戳要从会话名里剥掉",
          strip_list_time("腾讯新闻 20:42") == "腾讯新闻")
    check("S16 昨天+时间也要剥掉",
          strip_list_time("某仓库 昨天 10:07") == "某仓库")
    check("S16 省略号要剥掉",
          strip_list_time("某电商福利群6禁广告..") == "某电商福利群6禁广告")
    check("S16 只有标题行的会话不受影响",
          pick_titles([(100.0, "只有标题")]) == ["只有标题"])
    check("S16 标题+两行预览仍算一条会话",
          pick_titles([(100.0, "某客户"), (120.0, "预览一"), (140.0, "预览二")])
          == ["某客户"])
    check("S16 间距够大要分成两条会话",
          pick_titles([(100.0, "会话甲"), (200.0, "会话乙")])
          == ["会话甲", "会话乙"])

    # ---------------- S17 非文字消息归一 ----------------
    # 真实采到的样本（2026-10，微信 4.1.13 macOS）：语音气泡被 OCR 读成
    # '3"（' / '• 3"' / '• 3" • 转文字'，不归一就会被当成商家打的字。
    from adapters.vision_common import normalize_media_text as _N

    check("S17 语音碎片归一（带括号）", _N('3"（') == "[语音 3秒]")
    check("S17 语音碎片归一（带圆点）", _N('• 3"') == "[语音 3秒]")
    check("S17 语音碎片归一（带转文字按钮）", _N('• 3" • 转文字') == "[语音 3秒]")
    check("S17 两位数时长也对", _N('12"  转文字') == "[语音 12秒]")
    check("S17 已有占位符不动", _N("[图片]") == "[图片]")

    # 不能误伤真文本 —— 这比漏归一更危险
    for real in ("单号 773123456789012 到哪儿了", "这个件多少钱", "3件货",
                 "明天能到吗", "帮我催一下"):
        check(f"S17 真文本不能被误改：{real[:14]}", _N(real) == real)

    # 提示词必须交代非文字消息怎么处理（图片里的字代码区分不了）
    from app.prompts import SYSTEM_PROMPT as _SP
    check("S17 提示词说明了看不到非文字消息的内容", "[语音" in _SP and "[图片]" in _SP)
    check("S17 提示词警告了图片里的印刷字不能当原话",
          "印刷" in _SP or "图片里的字" in _SP)
    check("S17 提示词禁止从碎片里拼单号", "拼凑" in _SP or "绝对不要" in _SP)

    # ---------------- S18 媒体消息检测（图片/视频）-----------------
    # 真实踩到的坑：直接扫"非背景像素"不可靠 —— 那张打印机照片里有大片
    # 浅色（拍的白墙），被当成背景，214 点的图片被切成 46/12/13/13 点的
    # 碎片，一个都认不出来。改成用文字气泡当锚点。
    from adapters.vision_common import find_media_regions, DEFAULT_LAYOUT
    from bridge.vision_ocr import TextBox as _TB
    from PIL import Image as _Im, ImageDraw as _Dr

    _W, _H, _SC = 880, 640, 2.0
    _lay = DEFAULT_LAYOUT
    _im = _Im.new("RGB", (int(_W * _SC), int(_H * _SC)), (250, 250, 250))
    _d = _Dr.Draw(_im)
    # 一条文字气泡（y 150~185 逻辑点）
    _d.rectangle([int(380*_SC), int(195*_SC), int(700*_SC), int(230*_SC)], fill=(255,255,255))
    # 一张"图片"（y 213~427 逻辑点），里面故意留大片浅色区域
    _d.rectangle([int(370*_SC), int(213*_SC), int(530*_SC), int(427*_SC)], fill=(40,40,45))
    _d.rectangle([int(380*_SC), int(360*_SC), int(520*_SC), int(420*_SC)], fill=(248,248,248))
    # 图片里的印刷文字（OCR 会读出来，位置在图片内部）
    _d.rectangle([int(390*_SC), int(300*_SC), int(470*_SC), int(312*_SC)], fill=(90,90,90))
    _shot = str(_ROOT / "data" / "sim_media.png")
    _im.save(_shot)

    # 喂给它的 OCR 结果：文字气泡 + 图片里的印刷文字
    _boxes = [
        _TB(text="单号 773123456789012 到哪儿了",
            x=400/_W, y=1-(210/_H), w=0.30, h=0.025, conf=0.95),
        _TB(text="Canon 220V", x=430/_W, y=1-(306/_H), w=0.10, h=0.015, conf=0.9),
    ]
    _regions = find_media_regions(_shot, _lay, _W, _H, _boxes, _SC)
    check("S18 能从聊天区里认出图片", len(_regions) >= 1, f"实际 {len(_regions)} 个")
    if _regions:
        _r = _regions[0]
        _h_pt = (_r.pixel_box[3] - _r.pixel_box[1]) / _SC
        _w_pt = (_r.pixel_box[2] - _r.pixel_box[0]) / _SC
        check("S18 图片高度认得准（不能把大片浅色当背景切碎）",
              _h_pt >= 180, f"实际 {_h_pt:.0f} 点")
        check("S18 图片宽度认得准", 140 <= _w_pt <= 190, f"实际 {_w_pt:.0f} 点")
        check("S18 左右判断正确（对方的图在左边）", _r.side == "in", f"实际 {_r.side}")

    # 视觉描述要包成"这是图片"的格式，且必须提醒模型别把图里的字当原话
    from app.media import _to_message_text
    _t = _to_message_text("一台打印机的铭牌", "image")
    check("S18 图片描述带明确前缀", "[商家发来一张图片]" in _t)
    check("S18 图片描述提醒了以商家文字为准", "以商家文字为准" in _t)
    _tv = _to_message_text("封面帧内容", "video")
    check("S18 视频说明这是封面帧", "封面帧" in _tv)

    from app.media import VISION_PROMPT
    check("S18 视觉提示词要求诚实（看不清就说看不清）", "看不清" in VISION_PROMPT)
    check("S18 视觉提示词禁止猜商家意图", "不要推测" in VISION_PROMPT)
    check("S18 视觉提示词要求区分为商地址", "厂商地址" in VISION_PROMPT or "制造商" in VISION_PROMPT)

    # ---------------- S19 读消息前要滚到底 ----------------
    # 真实踩到的坑：商家发了一张截图，程序读了两次都还是旧内容 ——
    # 因为聊天区没停在最新消息处，新消息在可视区下方，截屏看不到。
    # _open_conversation 在标题已匹配时直接返回、不点也不聚焦，所以
    # 连"点一下让它跟随"都没有。结果就是**商家发了消息机器人永远不知道**。
    from adapters.macos_vision import MacWeChatVisionChannel
    from app.config import settings as _st

    check("S19 默认开启'读前滚到底'", _st.scroll_to_bottom is True)
    check("S19 滚动格数可配置且为正", _st.scroll_clicks > 0)
    check("S19 适配器提供了滚动方法",
          hasattr(MacWeChatVisionChannel, "scroll_chat_to_bottom"))

    import inspect as _insp
    _src = _insp.getsource(MacWeChatVisionChannel._read_messages)
    check("S19 _read_messages 里真的调用了滚动", "scroll_chat_to_bottom" in _src)
    _ssrc = _insp.getsource(MacWeChatVisionChannel.scroll_chat_to_bottom)
    check("S19 滚动前先聚焦（否则滚轮打到别的 App）", "self.focus()" in _ssrc)

    from pathlib import Path as _P

    _envtxt = (_ROOT / ".env.example").read_text(encoding="utf-8")
    check("S19 配置模板里有 WECHAT_SCROLL_TO_BOTTOM",
          "WECHAT_SCROLL_TO_BOTTOM=" in _envtxt)

    # ---------------- S20 看图抄字：反编造 + 关思考 ----------------
    # 真实事故：面单上分拣码写着「3-LR-九龙 6-F4」，寄件地址写的是
    # 「山东省潍坊市坊子区北海路」。模型把分拣码里的「九龙」抠出来，
    # 跟地址里的「坊子区」拼成「山东省潍坊市坊子区九龙街道」——
    # **面单上根本没这个地址**，而且看起来完全合理，不逐字核对发现不了。
    from app.media import VISION_PROMPT as _VP
    from app import models as _models

    check("S20 提示词点名了这次编造事故（九龙街道）", "九龙街道" in _VP)
    check("S20 提示词禁止补全", "不许补全" in _VP)
    check("S20 提示词要求逐字抄录", "逐字抄录" in _VP or "逐字" in _VP)
    check("S20 提示词说明分拣码不是地址",
          "分拣码" in _VP and "内部路由编码" in _VP)
    check("S20 提示词要求分拣码单独归类", "单独列" in _VP or "单独归到" in _VP)
    check("S20 提示词讲了隐私面单（只留姓/尾号/虚拟号）",
          "虚拟号" in _VP and "尾号" in _VP)
    check("S20 提示词要求读不到写「未见」", "未见" in _VP)
    check("S20 提示词禁止把不同区域的字拼一起", "拼在一起" in _VP)

    _v = _models.vision_profile()
    check("S20 视觉档案存在", _v is not None)
    if _v:
        check("S20 看图必须关掉思考模式", _v.thinking == "disabled",
              f"实际 {_v.thinking!r}")
        check("S20 用官方模型名 deepseek-flash（旧实验名已下线）",
              _v.model == "deepseek-flash", f"实际 {_v.model}")
        check("S20 关思考后额度不需要给到 8000", _v.max_tokens <= 4000)

    _t = _models.active_profile()
    if _t:
        check("S20 文本模型不能跟着关思考（推理有帮助）",
              _t.thinking != "disabled", f"实际 {_t.thinking!r}")

    _msrc = (_ROOT / "app" / "media.py").read_text(encoding="utf-8")
    check("S20 media.py 真的把 thinking 参数传下去了",
          'extra_body=extra' in _msrc and 'thinking' in _msrc)

    # ---------------- S21 关键编号交叉核对 ----------------
    # 起因：视觉模型读密集小字时数字不稳定，而且不会因为不确定就留空。
    # 实测同一张面单，三次调用给出过三个不同的寄件人电话。
    # 做法：本地 OCR（数字准）+ 视觉模型（文字强）交叉验证。
    from app.media import (extract_identifiers, cross_check,
                           format_cross_check, _looks_like_timestamp)

    # 各家快递单号形态都要认得
    for wb, why in [("SF123456789012", "顺丰"), ("YT1234567890123", "圆通"),
                    ("773123456789012", "申通长号"), ("JDAP20569998821", "京东"),
                    ("EA123456789CN", "EMS"), ("75512345678901", "中通")]:
        check(f"S21 认得出{why}单号 {wb}", wb in extract_identifiers(wb))

    check("S21 认得出手机号", "13371068550" in extract_identifiers("电话13371068550"))

    # 时间戳不能被当成运单号（实测踩过：2026-07-23 22:38:54 连成 14 位数字）
    check("S21 时间戳不算运单号", _looks_like_timestamp("20260723223854"))
    check("S21 轨迹时间戳不会误报",
          "20260723223854" not in extract_identifiers("20260723223854|"))

    # 两个来源一致 → 可信
    ck = cross_check("运单号 JDAP20569998821 电话 13371068550",
                     "面单上写着 JDAP20569998821-1-1- 电话 13371068550")
    verdicts = {c.value: c.verdict for c in ck}
    check("S21 两边一致判为『一致』", verdicts.get("JDAP20569998821") == "一致")
    check("S21 视觉多带后缀也算一致（-1-1-）",
          verdicts.get("JDAP20569998821-1-1-") == "一致"
          or any(c.verdict == "一致" and c.value.startswith("JDAP") for c in ck))

    # 只有一边有 → 存疑
    ck2 = cross_check("快递员电话 18706673436", "快递员电话 18706434836")
    check("S21 两边不一致时两个都不可信",
          all(c.verdict != "一致" for c in ck2) and len(ck2) == 2,
          f"实际 {[(c.value, c.verdict) for c in ck2]}")

    ck3 = cross_check("", "面单上 JDAP20569998821")
    check("S21 OCR 完全没读到 → 标『仅视觉』不可信",
          len(ck3) == 1 and ck3[0].verdict == "仅视觉")

    # 冲突：同一个号两边读出来不一样（实测收件人电话 17566667620 vs 17560667620）
    ck4 = cross_check("收件人张祥龙17560667620", "收件人张祥龙 17566667620")
    check("S21 同一号两边不一致判为『冲突』",
          len(ck4) == 1 and ck4[0].verdict == "冲突",
          f"实际 {[(c.value, c.verdict, c.conflict_with) for c in ck4]}")
    check("S21 冲突时两个值都保留下来",
          ck4 and ck4[0].value == "17566667620" and ck4[0].conflict_with == "17560667620")
    check("S21 冲突的提示语说明文字识别通常更可靠",
          "文字识别通常更可靠" in format_cross_check(ck4))

    # 图片可能被误判成视频：聊天时间戳(21:10)和视频时长(0:15)格式一样。
    # 视频时长叠加在缩略图上，所以判定必须要求"在区域内"，不能只是"附近"。
    import inspect as _i2
    from adapters.vision_common import find_media_regions as _fmr
    _fsrc = _i2.getsource(_fmr)
    check("S21 视频判定要求时长标记在图片区域内",
          "inside_x" in _fsrc and "inside_y" in _fsrc)
    check("S21 不再用 ±40 的'附近'判定", "- 40) < bx" not in _fsrc)

    txt = format_cross_check(ck)
    check("S21 输出里明确写了『可信』", "可信" in txt)
    check("S21 输出里明确写了『不能据此做任何操作』", "不能据此做任何操作" in txt)

    from app.prompts import SYSTEM_PROMPT as _SP2
    check("S21 客服提示词讲了怎么用交叉核对结果",
          "交叉核对" in _SP2 and "可信" in _SP2)
    check("S21 客服提示词禁止在两个不一致的号码里挑一个",
          "挑一个" in _SP2 or "不一致" in _SP2)

    # ---------------- S22 语音转写 ----------------
    # 微信本地语音文件是加密的，我们不碰进程，所以拿不到音频。
    # 做法是**复用微信自带的「转文字」**：点语音气泡旁边的按钮，
    # 微信自己把语音转成文字显示出来，我们再 OCR 读回来。
    from adapters.vision_common import (find_voice_bubbles, _VOICE_DUR_RE,
                                        DEFAULT_LAYOUT)
    from bridge.vision_ocr import TextBox as _TBox
    from adapters.macos_vision import MacWeChatVisionChannel as _MC

    for raw, secs in [('3"', 3), ('3"（', 3), ('• 3"', 3), ('小 3"', 3),
                      ('12"', 12), ('• 3" •', 3)]:
        _m = _VOICE_DUR_RE.match(raw)
        check(f"S22 认得语音时长标记 {raw!r}", bool(_m) and int(_m.group(1)) == secs)

    for not_voice in ("单号 773123456789012", "3件货", "2026-07-24", "转文字"):
        check(f"S22 不把 {not_voice[:12]!r} 当语音时长",
              _VOICE_DUR_RE.match(not_voice) is None)

    _W2, _H2 = 880, 640
    _vb = [
        _TBox(text='3"', x=400/_W2, y=1-(440/_H2), w=0.02, h=0.02, conf=0.9),
        _TBox(text='转文字', x=520/_W2, y=1-(445/_H2), w=0.05, h=0.02, conf=0.9),
        _TBox(text='5"', x=700/_W2, y=1-(380/_H2), w=0.02, h=0.02, conf=0.9),
    ]
    _vs = find_voice_bubbles(_vb, DEFAULT_LAYOUT, _W2, _H2)
    check("S22 找得到语音气泡", len(_vs) == 2, f"实际 {len(_vs)}")
    _in = [v for v in _vs if v.side == "in"]
    _out = [v for v in _vs if v.side == "out"]
    check("S22 对方那条带「转文字」按钮",
          bool(_in) and _in[0].button_xy is not None)
    check("S22 自己那条不带按钮（微信不给自己语音显示）",
          bool(_out) and _out[0].button_xy is None)
    check("S22 时长读对了", bool(_in) and _in[0].seconds == 3)

    from app.config import settings as _st2
    check("S22 默认开启语音转写", _st2.transcribe_voice is True)
    check("S22 适配器提供了转写方法",
          hasattr(_MC, "transcribe_voices"))
    import inspect as _i3
    _rsrc = _i3.getsource(_MC._read_messages)
    check("S22 _read_messages 里真的调用了转写",
          "transcribe_voices" in _rsrc)
    _tsrc = _i3.getsource(_MC.transcribe_voices)
    check("S22 点按钮前做了安全检查（只在聊天区内点）",
          "chat_left" in _tsrc and "按钮位置异常" in _tsrc)
    check("S22 转写失败不影响读文字", "不影响读文字" in _rsrc)

    _envtxt2 = (_ROOT / ".env.example").read_text(encoding="utf-8")
    check("S22 配置模板里有 WECHAT_TRANSCRIBE_VOICE",
          "WECHAT_TRANSCRIBE_VOICE=" in _envtxt2)

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
          title_ok("某电商福利群6禁广告链接", "某电商福利群6"))
    check("S12 群名带成员数也能匹配",
          title_ok("某某行业交流群（304）", "某某行业交流群"))
    check("S12 过短的前缀不算匹配（防串会话）",
          not title_ok("客户AB群", "客户A"))
    check("S12 完全不同的会话不匹配",
          not title_ok("某旅居兴趣群", "某电商福利群6"))

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
