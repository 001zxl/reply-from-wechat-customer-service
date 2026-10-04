"""微信接入适配层的统一契约。

任何适配器都必须满足：
- 消息 ID 稳定且持久（不能拿"最新一条文本"当 ID）
- 发送结果必须区分 sent / failed / unknown
- unknown 绝不能自动重发
- 发送前必须能确认目标会话没串
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.schemas import IncomingMessage

SendStatus = Literal["sent", "failed", "unknown"]


@dataclass
class SendResult:
    status: SendStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "sent"


@runtime_checkable
class WeChatAdapter(Protocol):
    channel: str

    async def verify_target(self, channel_chat_id: str) -> bool:
        """发送前确认目标与绑定会话一致。返回 False 就必须暂停该会话。"""
        ...

    async def send(self, channel_chat_id: str, text: str) -> SendResult:
        ...


class AdapterTargetMismatch(Exception):
    """目标校验失败。调用方必须暂停会话并通知人工，不能硬发。"""


__all__ = [
    "IncomingMessage", "SendResult", "SendStatus",
    "WeChatAdapter", "AdapterTargetMismatch",
]
