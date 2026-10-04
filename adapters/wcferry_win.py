"""PC 微信 Hook 通道 —— 非官方，仅 Windows，实验性，有封号风险。

先读这段再决定用不用：
1. 个人微信没有任何官方 API。这个通道依赖第三方项目（如 WeChatFerry）对
   微信 PC 客户端的进程注入，属于非官方手段，违反《微信个人帐号使用规范》，
   账号可能被限制或封禁。
2. 它强绑定微信 PC 客户端的具体版本，微信一升级就可能整体失效。
3. 它只能在 Windows 上跑，且要求客户端保持登录、窗口不被锁屏/远程桌面挂起。
4. 本文件不包含任何绕过风控、伪装、账号轮换的实现，也不会加。

因此它的定位只有一个：在测试机上验证"客服核心 + 群聊编排"能不能跑通。
生产环境请优先使用官方通道（企业微信 / 微信客服）。

部署：pip install wcferry  （Windows，Python 3.10~3.12）
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Any, Optional

from app.schemas import IncomingMessage, now_iso
from .base import SendResult


class WcFerryUnavailable(RuntimeError):
    pass


class WcFerryChannel:
    """收消息在后台线程拉取；发消息加全局锁，避免多会话争抢客户端焦点。"""

    channel = "wcferry_win"
    _send_lock = threading.Lock()

    def __init__(self, bot_name: str = "") -> None:
        if platform.system() != "Windows":
            raise WcFerryUnavailable("PC 微信 Hook 只能在 Windows 上运行")
        try:
            from wcferry import Wcf  # type: ignore
        except ImportError as exc:
            raise WcFerryUnavailable("未安装 wcferry：pip install wcferry") from exc

        self.bot_name = bot_name
        self._wcf = Wcf()
        self._queue: list[IncomingMessage] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---------------- 收 ----------------
    def _loop(self) -> None:
        try:
            self._wcf.enable_receiving_msg()
        except Exception:
            return
        while not self._stop.is_set():
            try:
                raw = self._wcf.get_msg()
            except Exception:
                time.sleep(0.5)
                continue
            msg = self._normalize(raw)
            if msg is not None:
                with self._lock:
                    self._queue.append(msg)

    def _normalize(self, raw: Any) -> Optional[IncomingMessage]:
        if raw is None:
            return None
        is_self = bool(getattr(raw, "from_self", False)) or bool(
            getattr(raw, "is_self", lambda: False)()
        )
        if is_self:
            return None
        mtype = getattr(raw, "type", None)
        if mtype is not None and mtype != 1:
            return None                       # 只处理文本
        content = str(getattr(raw, "content", "") or "").strip()
        if not content:
            return None

        roomid = str(getattr(raw, "roomid", "") or "")
        sender = str(getattr(raw, "sender", "") or "")
        # 群消息里 content 常带 "wxid:\n正文" 前缀，剥掉
        if roomid and ":\n" in content:
            head, _, tail = content.partition(":\n")
            if head.startswith("wxid_") or head.endswith("@chatroom"):
                content = tail.strip()

        is_group = bool(roomid)
        chat_id = roomid or sender
        mentioned = (not is_group) or (
            bool(self.bot_name) and (f"@{self.bot_name}" in content or self.bot_name in content)
        )
        raw_id = str(getattr(raw, "id", "") or "")
        event_id = raw_id or f"wcf-{chat_id}-{getattr(raw, 'ts', int(time.time()))}-{abs(hash(content)) % 10**10}"

        return IncomingMessage(
            event_id=event_id,
            channel=self.channel,
            conversation_id=chat_id,
            channel_chat_id=chat_id,
            sender_id=sender,
            sender_name="",
            text=content,
            is_group=is_group,
            is_self=False,
            mentioned_bot=mentioned,
            received_at=now_iso(),
        )

    def poll(self) -> list[IncomingMessage]:
        with self._lock:
            out, self._queue = self._queue, []
        return out

    def close(self) -> None:
        self._stop.set()
        try:
            self._wcf.disable_receiving_msg()
        except Exception:
            pass

    # ---------------- 发 ----------------
    async def verify_target(self, channel_chat_id: str) -> bool:
        """校验聊天窗口当前打开的就是目标会话。

        这一条必须做，因为 PC Hook 是"操作当前窗口"，一旦定位错会话就会把
        甲的消息发给乙。这里调用客户端查询会话名，与绑定信息比对。
        """
        try:
            contacts = self._wcf.get_contacts()
        except Exception:
            return False
        for c in contacts or []:
            if str(c.get("wxid") or c.get("roomid") or "") == channel_chat_id:
                return True
        return False

    async def send(self, channel_chat_id: str, text: str) -> SendResult:
        if not await self.verify_target(channel_chat_id):
            return SendResult("failed", "目标校验失败，已拒绝发送")
        try:
            with self._send_lock:
                ok = self._wcf.send_text(text, channel_chat_id)
                time.sleep(0.8)          # 人一样的节奏，不要瞬间连发
        except Exception as exc:
            return SendResult("unknown", f"发送异常，结果未知：{type(exc).__name__}")
        return SendResult("sent" if ok in (0, None, True) else "failed", f"return={ok}")
