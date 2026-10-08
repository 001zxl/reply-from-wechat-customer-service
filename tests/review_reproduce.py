"""外部审查的 13 个反例 —— 现在当**验收测试**用。

来源：外部对 ef92c01 的审查报告（REVIEW.md）+ review_reproduce.py。
语义已反转：这里有任何一个 "REPRODUCED"，就说明对应的缺口**还没修好**。
全部 NOT REPRODUCED 才算通过。

不同之处（相对审查方的原脚本）：
  · 只读，不连微信、不调模型 API（发送对象是内存里的 Recorder）
  · DB_PATH 指向临时库
  · 退出码：有任何一个反例仍能复现 → 返回 1

Run: .venv/bin/python tests/review_reproduce.py
"""

import asyncio
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent   # 仓库根（本文件在 tests/ 下）
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
tmp = tempfile.TemporaryDirectory(prefix="wechat-review-")
os.environ.update(DB_PATH=str(Path(tmp.name) / "review.db"),
                  WECHAT_CHANNEL="mock", LLM_BACKEND="mock", DRY_RUN="1",
                  LOGISTICS_PROVIDER="mock", QUIET_HOURS="")

from app import db, policy
import app.pipeline as pl
from app.llm import CustomerServiceLLM, LLMOutcome
from app.schemas import Action, Decision, IncomingMessage, Intent
from app.mock_llm import MockLLM
from adapters.base import SendResult
from adapters.vision_common import _is_system_line, title_ok

results = []

def record(name, reproduced, detail):
    results.append({"name": name, "reproduced": bool(reproduced), "detail": detail})
    print(("REPRODUCED " if reproduced else "NOT REPRODUCED ") + name)
    print("  " + json.dumps(detail, ensure_ascii=False))

class Recorder:
    """In-memory test double; never imports or invokes a real channel."""
    def __init__(self, result="sent"):
        self.calls = []
        self.result = result

    async def verify_target(self, target):
        return True

    async def send(self, target, text):
        self.calls.append((target, text))
        return SendResult(self.result, "IN-MEMORY TEST ONLY")

def conv(name):
    db.upsert_conversation(name, "mock", name, name, "review-merchant", "auto")
    return name

def incoming(c, event, text, sender="A", group=False, mentioned=True):
    return IncomingMessage(event_id=event, channel="mock", conversation_id=c,
                           channel_chat_id=c, sender_id=sender, sender_name=sender,
                           text=text, is_group=group, mentioned_bot=mentioned)

async def main() -> int:
    db.init_db()

    # Simulate the missing macOS packages on a Windows installation.
    script = '''
import sys, importlib.abc
class MissingMac(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'Quartz', 'Vision', 'Foundation'}:
            raise ModuleNotFoundError("No module named '" + fullname + "'")
sys.meta_path.insert(0, MissingMac())
import adapters.windows_vision
'''
    p = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
    record("Windows adapter imports macOS-only modules", p.returncode != 0 and "Quartz" in p.stderr,
           p.stderr.splitlines()[-5:])

    texts = ["今天客户一直没收到，帮我查一下", "昨天发的那票客户不要了", "星期一能送到吗"]
    dropped = [t for t in texts if _is_system_line(t)]
    record("Business messages mistaken for timestamp lines", len(dropped) == len(texts), dropped)

    accepted = title_ok("杭州电商客服二群", "杭州电商客服")
    record("Different conversation accepted by prefix identity check", accepted,
           {"actual": "杭州电商客服二群", "expected": "杭州电商客服", "accepted": accepted})

    c = conv("sender-context")
    ids = [db.save_incoming(incoming(c, "sender-a-001", "773123456789012", "客服甲", True)),
           db.save_incoming(incoming(c, "sender-b-001", "这个退回来", "客服乙", True))]
    history = db.history_before(c, 999999)
    record("Group sender identities dropped from model history",
           all("客服甲" not in h["content"] and "客服乙" not in h["content"] for h in history), history)

    # White list is replaced only within the test process; no real chat is added.
    c = conv("group-followup")
    pipe = pl.Pipeline(Recorder(), MockLLM())
    rule = {"mode": "review", "require_mention_in_group": True}
    with patch.object(policy, "conversation_rule", return_value=rule):
        await pipe.submit(incoming(c, "group-number-001", "773123456789012", group=True, mentioned=False))
        response = await pipe.submit(incoming(c, "group-follow-001", "这票不要了退回来", group=True, mentioned=False))
    await pipe.stop()
    record("Group followup not queued after standalone waybill", response.reason.startswith("不回复"), response.reason)

    for change in ("new_message", "takeover"):
        c = conv("during-delay-" + change)
        channel = Recorder()
        pipe = pl.Pipeline(channel, MockLLM())
        draft_id = db.create_draft(c, "review-batch", "reply", "business_inquiry", "旧回复", [], status="approved")
        initial = db.version_of(c)
        async def during_sleep(delay):
            if change == "new_message":
                db.bump_version(c)
            else:
                db.set_takeover(c, "2099-01-01T00:00:00+08:00")
        test_settings = dataclasses.replace(pl.settings, dry_run=False)
        with patch.object(pl, "settings", test_settings), patch.object(pl.asyncio, "sleep", during_sleep):
            status = await pipe._deliver(draft_id=draft_id, conv_id=c, channel_chat_id=c,
                                         text="旧回复", expect_version=initial, manual=False)
        record("Sends despite " + change + " during delay", bool(channel.calls),
               {"send_calls": len(channel.calls), "status": status, "version": db.version_of(c),
                "human_taken_over": db.is_human_taken_over(c)})

    c = conv("unknown-resend")
    channel = Recorder("unknown")
    pipe = pl.Pipeline(channel, MockLLM())
    draft_id = db.create_draft(c, "unknown-batch", "reply", "business_inquiry", "同一条草稿", [], status="draft")
    statuses = [await pipe.deliver_manual(draft_id), await pipe.deliver_manual(draft_id)]
    record("Unknown delivery can be resent through normal send endpoint", len(channel.calls) == 2,
           {"send_calls": len(channel.calls), "statuses": statuses})

    c = conv("concurrent-manual-send")
    class SlowRecorder(Recorder):
        async def send(self, target, text):
            self.calls.append((target, text))
            await asyncio.sleep(0.02)
            return SendResult("sent", "IN-MEMORY TEST ONLY")
    channel = SlowRecorder()
    pipe = pl.Pipeline(channel, MockLLM())
    draft_id = db.create_draft(c, "concurrent-batch", "reply", "business_inquiry",
                               "同一条草稿", [], status="draft")
    statuses = await asyncio.gather(pipe.deliver_manual(draft_id), pipe.deliver_manual(draft_id),
                                    return_exceptions=True)
    record("Concurrent manual approval sends one draft twice", len(channel.calls) == 2,
           {"send_calls": len(channel.calls), "statuses": [str(s) for s in statuses]})

    llm = object.__new__(CustomerServiceLLM)
    d = Decision(action=Action.reply, intent=Intent.other,
                 waybill_numbers=["773999999999999"], reply="773999999999999 已经签收了。")
    d = llm._enforce_grounding(d, [], [], "帮我查一下", [])
    verdict = policy.check_send_policy(mode="auto", takeover_until=None, action=d.action,
                intent=d.intent, reply=d.reply, logistics_real=False, auto_sent_last_minute=0,
                last_auto_sent_at=None, inbound_text="帮我查一下")
    record("Fabricated waybill and status survive grounding and send policy",
           "773999999999999" in d.reply and verdict.allowed,
           {"decision": d.model_dump(mode="json"), "allowed": verdict.allowed})

    # The demo ERP is enabled by the shipped config. Execute only this local demo.
    from app.tools import ToolRegistry
    c = conv("demo-erp")
    tool_result = await ToolRegistry(c).execute("call_demo_erp",
                         {"action": "query_waybill", "waybill_no": "773123456789012"})
    class FixedToolLLM:
        async def decide(self, **kwargs):
            return LLMOutcome(decision=Decision(action=Action.reply, intent=Intent.other,
                reply="负责这票的业务员是张伟，归属杭州余杭一部。"), evidence=[tool_result.evidence])
    pipe = pl.Pipeline(Recorder(), FixedToolLLM())
    outcome, evidence, mock_flag = await pipe._think(c, "谁负责这票", [], db.get_conversation(c), False, "erp-batch")
    verdict = policy.check_send_policy(mode="auto", takeover_until=None,
                action=outcome.decision.action, intent=outcome.decision.intent,
                reply=outcome.decision.reply, logistics_real=False, auto_sent_last_minute=0,
                last_auto_sent_at=None, inbound_text="谁负责这票")
    record("Enabled demo ERP is not covered by mock-data gate",
           tool_result.evidence.ok and not mock_flag and verdict.allowed,
           {"evidence": evidence, "mock_flag": mock_flag, "allowed": verdict.allowed})

    # 发送回读确认。
    #
    # ★ 这一条相对审查方原脚本做了**改动**，说明一下为什么不是"改测试让它过"：
    #   原脚本把当时的**生产表达式**抄了一份内联在这里：
    #       any(m.side == "out" and _similar(new[:14], m.text[:14]) for m in observed)
    #   它想证明的是"旧气泡能满足新发送的成功判定"。
    #   修复之后，生产代码里**已经不存在这个表达式**了 —— 改成
    #   "点发送前先数一遍有几条同文气泡，发送后必须多出来一条"
    #   （adapters/vision_common.count_bubbles + macos_vision._out_texts）。
    #   所以这里改成直接调用**真正的生产判定**，否则这条测试永远复现，
    #   而它测的是一个已经不存在的表达式，毫无意义。
    #   旧表达式本身仍然是不安全的，下面同时断言了这一点，作为对照留档。
    from adapters.vision_common import Observed, _similar, count_bubbles
    old = "收到你的咨询，我帮你看一下旧单。"
    new = "收到你的咨询，我帮你看一下新单。"
    before = [old]

    # 旧表达式（不安全，留档）：只比前 14 字，旧气泡照样"确认"成功
    legacy_unsafe = any(
        m.side == "out" and _similar(new[:14], m.text[:14]) for m in [Observed("out", old)]
    )
    # 新生产判定：屏幕上没有**新增**的同文气泡 → 不算发送成功
    produced_nothing_new = count_bubbles(before, new)       # 发送前：0 条
    after_without_new = before                              # 点了发送但没发出去
    wrongly_confirmed = count_bubbles(after_without_new, new) > produced_nothing_new
    # 正例：真发出去之后应该能确认
    after_with_new = before + [new]
    correctly_confirmed = count_bubbles(after_with_new, new) > produced_nothing_new
    record("Old outgoing message can satisfy new-send acknowledgement",
           wrongly_confirmed or not correctly_confirmed,
           {"legacy_expression_would_confirm": legacy_unsafe,
            "new_logic_wrongly_confirms": wrongly_confirmed,
            "new_logic_confirms_real_send": correctly_confirmed,
            "old_visible_message": old, "new_unsent_message": new})

    # Actual poll logic, with all GUI I/O replaced by synthetic observations.
    from adapters.windows_vision import WindowsWeChatVisionChannel
    adapter = object.__new__(WindowsWeChatVisionChannel)
    adapter.watch = ["synthetic-group"]
    adapter._last_msgs = {"synthetic-group": ["in|旧消息"]}
    adapter.compare_failures = adapter.dropped_hint = 0
    adapter._title_matches = lambda chat: True
    adapter.read_messages = lambda chat: [Observed("in", "旧消息"), Observed("in", "大家吃饭了吗", "小王")]
    with patch("adapters.windows_vision._next_seq", return_value=999):
        events = adapter.poll()
    verdict = policy.should_respond(events[0], True)
    record("Desktop poll marks unmentioned group chatter as mentioned", events[0].mentioned_bot and verdict.allowed,
           {"text": events[0].text, "is_group": events[0].is_group,
            "mentioned_bot": events[0].mentioned_bot, "should_respond": verdict.allowed})

    report = {"commit": os.environ.get("REVIEW_COMMIT", "working-tree"), "offline_only": True,
              "findings": results, "reproduced": sum(r["reproduced"] for r in results)}
    (Path(os.environ.get("REVIEW_OUT", str(ROOT / "review_results.json")))).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("\n仍能复现的缺口:", report["reproduced"], "/", len(results))
    if report["reproduced"] == 0:
        print("✅ 13 个反例全部被正确阻止")
    else:
        print("❌ 还有缺口没修：")
        for r in results:
            if r["reproduced"]:
                print("   -", r["name"])
    return 1 if report["reproduced"] else 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
