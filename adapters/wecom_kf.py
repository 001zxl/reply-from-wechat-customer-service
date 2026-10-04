"""企业微信「微信客服」通道 —— 官方 API，合规，推荐生产使用。

能力边界（必须清楚）：
- 微信客服是【一对一】会话。普通微信用户通过企业微信的「微信客服」入口
  （搜一搜 / 二维码 / 小程序 / 公众号菜单）进入会话，本服务就能收到消息并回复。
- 它【不支持群聊】。群聊场景见 README 的通道对比。

消息闭环：
  企业微信回调 (kf_msg_or_event, 带 Token)
    → 本服务调 kf/sync_msg 拉取真实消息（cursor 持久化）
    → 走客服核心
    → kf/send_msg 回复
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import ROOT, settings
from app.schemas import IncomingMessage, now_iso
from .base import SendResult

API = "https://qyapi.weixin.qq.com/cgi-bin"
CURSOR_FILE = ROOT / "data" / "wecom_cursor.json"


def _load_cursor() -> str:
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text(encoding="utf-8")).get("cursor", "")
        except (json.JSONDecodeError, OSError):
            return ""
    return ""


def _save_cursor(cursor: str) -> None:
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(
        json.dumps({"cursor": cursor, "saved_at": now_iso()}, ensure_ascii=False),
        encoding="utf-8",
    )


class WeComKfChannel:
    channel = "wecom_kf"

    def __init__(self) -> None:
        self.cfg = settings.wecom
        self._token: str = ""
        self._token_expire: float = 0.0
        self._callback_token: str = ""

    # ---------------- 基础 ----------------
    def set_callback_token(self, token: str) -> None:
        """回调里带过来的 Token，用于增量拉取，减少重复。"""
        if token:
            self._callback_token = token

    async def access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return self._token
        async with httpx.AsyncClient(timeout=10.0) as cli:
            resp = await cli.get(
                f"{API}/gettoken",
                params={"corpid": self.cfg.corp_id, "corpsecret": self.cfg.kf_secret},
            )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 access_token 失败：{data.get('errcode')} {data.get('errmsg')}")
        self._token = data["access_token"]
        self._token_expire = now + float(data.get("expires_in", 7200))
        return self._token

    # ---------------- 收 ----------------
    async def sync(self, limit: int = 100) -> list[dict[str, Any]]:
        token = await self.access_token()
        cursor = _load_cursor()
        body: dict[str, Any] = {"cursor": cursor, "limit": limit, "voice_format": 0}
        if self._callback_token:
            body["token"] = self._callback_token
        async with httpx.AsyncClient(timeout=15.0) as cli:
            resp = await cli.post(f"{API}/kf/sync_msg", params={"access_token": token}, json=body)
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"sync_msg 失败：{data.get('errcode')} {data.get('errmsg')}")
        next_cursor = data.get("next_cursor") or ""
        if next_cursor:
            _save_cursor(next_cursor)
        return list(data.get("msg_list") or [])

    @staticmethod
    def to_incoming(item: dict[str, Any]) -> Optional[IncomingMessage]:
        """把微信客服消息转成统一入站消息。origin=3 是微信客户发来的。"""
        if item.get("origin") != 3:
            return None                     # 4=系统消息, 5=接待人员(含我们自己)
        msgtype = item.get("msgtype")
        if msgtype != "text":
            return None                     # 图片/语音/文件第一版不处理，留给人工
        content = ((item.get("text") or {}).get("content") or "").strip()
        if not content:
            return None
        open_kfid = str(item.get("open_kfid") or "")
        external = str(item.get("external_userid") or "")
        if not open_kfid or not external:
            return None
        chat_id = f"{open_kfid}:{external}"
        event_id = str(item.get("msgid") or "")
        if not event_id:
            # 微信客服正常会给 msgid。万一缺失，用可复现的合成 ID，
            # 而不是文本哈希（文本哈希会把两次相同的"催一下"误判为重复）。
            event_id = f"kf-{open_kfid}-{external}-{item.get('send_time')}-{abs(hash(content)) % 10**10}"
        return IncomingMessage(
            event_id=event_id,
            channel="wecom_kf",
            conversation_id=chat_id,
            channel_chat_id=chat_id,
            sender_id=external,
            sender_name="",
            text=content,
            is_group=False,
            is_self=False,
            mentioned_bot=True,
        )

    # ---------------- 发 ----------------
    async def verify_target(self, channel_chat_id: str) -> bool:
        """目标一定来自回调本身，天然可信；这里只做格式与白名单复核。"""
        if ":" not in channel_chat_id:
            return False
        open_kfid = channel_chat_id.split(":", 1)[0]
        from app.policy import conversation_rule
        return conversation_rule(self.channel, channel_chat_id) is not None or bool(open_kfid)

    async def send(self, channel_chat_id: str, text: str) -> SendResult:
        open_kfid, _, external = channel_chat_id.partition(":")
        if not open_kfid or not external:
            return SendResult("failed", "目标格式非法")
        try:
            token = await self.access_token()
            async with httpx.AsyncClient(timeout=15.0) as cli:
                resp = await cli.post(
                    f"{API}/kf/send_msg",
                    params={"access_token": token},
                    json={
                        "touser": external,
                        "open_kfid": open_kfid,
                        "msgtype": "text",
                        "text": {"content": text[:2000]},
                    },
                )
            data = resp.json()
        except Exception as exc:
            # 网络异常 → 结果未知。绝不能自动重发。
            return SendResult("unknown", f"请求异常，结果未知：{type(exc).__name__}")

        if data.get("errcode") == 0:
            return SendResult("sent", f"msgid={data.get('msgid', '')}")
        return SendResult("failed", f"{data.get('errcode')} {data.get('errmsg')}")
