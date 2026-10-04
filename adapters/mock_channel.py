"""本地回环通道：不碰微信，用来把业务流程完整跑通。

发送结果写到 data/mock_outbox.jsonl，人工可以逐条核对，
所以它也是一个"发送内容审计日志"。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import ROOT
from .base import SendResult

OUTBOX = ROOT / "data" / "mock_outbox.jsonl"


class MockChannel:
    channel = "mock"

    async def verify_target(self, channel_chat_id: str) -> bool:
        return True

    async def send(self, channel_chat_id: str, text: str) -> SendResult:
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        with OUTBOX.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"to": channel_chat_id, "text": text},
                ensure_ascii=False,
            ) + "\n")
        return SendResult("sent", f"已写入 {OUTBOX.name}")
