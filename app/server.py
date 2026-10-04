"""FastAPI 入口：接微信回调、接本地投递、提供人工审核台 API。

启动：python -m uvicorn app.server:app --host 127.0.0.1 --port 8787
或：  python -m app.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import db, models, policy
from bridge import guard
from .config import ROOT, settings
from .pipeline import Pipeline, get_pipeline, set_pipeline
from .schemas import ConversationMode, IncomingMessage, now_iso
from .wecom_crypto import WeComCrypto, WeComCryptoError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("server")

app = FastAPI(title="申通网点微信客服助手", version="0.1.0")
DESK_HTML = Path(__file__).resolve().parent / "desk.html"


# ======================================================================
# 生命周期
# ======================================================================
@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    pipe = get_pipeline()
    await pipe.start()
    log.info("通道=%s 物流=%s 模型=%s 数据库=%s",
             settings.channel, settings.logistics.provider,
             settings.llm.model, settings.db_file())


@app.on_event("shutdown")
async def _shutdown() -> None:
    await get_pipeline().stop()


def require_token(authorization: str = Header(default="")) -> None:
    """统一鉴权依赖。放在依赖里执行，保证鉴权先于请求体校验。"""
    expected = f"Bearer {settings.app_token}"
    if not settings.app_token or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="接口令牌错误")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "channel": settings.channel,
        "logistics_provider": settings.logistics.provider,
        "llm_profile": settings.llm.profile_id,
        "llm_provider": settings.llm.provider_label,
        "llm_model": settings.llm.model,
        "llm_configured": bool(settings.llm.api_key),
        "wecom_configured": settings.wecom.ready,
    }


# ======================================================================
# 入站
# ======================================================================
@app.post("/ingest")
async def ingest(
    msg: IncomingMessage,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    """本地投递入口。测试脚本、PC Hook 桥接程序都走这里。"""
    result = await get_pipeline().submit(msg)
    return {
        "accepted": result.accepted,
        "reason": result.reason,
        "conversation_id": result.conversation_id,
        "queue_depth": result.queue_depth,
    }


# ======================================================================
# 企业微信「微信客服」回调
# ======================================================================
def _crypto() -> WeComCrypto:
    cfg = settings.wecom
    if not cfg.ready:
        raise HTTPException(status_code=503, detail="企业微信通道未配置")
    return WeComCrypto(cfg.callback_token, cfg.encoding_aes_key, cfg.corp_id)


@app.get("/webhook/wecom")
async def wecom_verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> PlainTextResponse:
    try:
        plain = _crypto().decrypt_echo(msg_signature, timestamp, nonce, echostr)
    except WeComCryptoError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PlainTextResponse(plain)


@app.post("/webhook/wecom")
async def wecom_callback(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> PlainTextResponse:
    raw = (await request.body()).decode("utf-8", "ignore")
    try:
        root = ET.fromstring(raw)
        encrypt = (root.findtext("Encrypt") or "").strip()
        if not encrypt:
            return PlainTextResponse("")
        crypto = _crypto()
        if not crypto.verify_signature(msg_signature, timestamp, nonce, encrypt):
            raise HTTPException(status_code=400, detail="回调签名不通过")
        plain = crypto.decrypt(encrypt)
        event = ET.fromstring(plain)
        if (event.findtext("Event") or "").lower() != "kf_msg_or_event":
            return PlainTextResponse("")
        token = (event.findtext("Token") or "").strip()
    except HTTPException:
        raise
    except (ET.ParseError, WeComCryptoError) as exc:
        log.warning("企业微信回调解析失败：%s", exc)
        return PlainTextResponse("")

    # 回调必须 5 秒内返回，拉取放后台
    asyncio.create_task(_pull_wecom(token))
    return PlainTextResponse("")


async def _pull_wecom(token: str = "") -> int:
    from adapters.wecom_kf import WeComKfChannel

    channel = WeComKfChannel()
    if token:
        channel.set_callback_token(token)
    try:
        items = await channel.sync()
    except Exception:
        log.exception("sync_msg 失败")
        return 0
    n = 0
    for item in items:
        msg = channel.to_incoming(item)
        if msg is None:
            continue
        res = await get_pipeline().submit(msg)
        if res.accepted:
            n += 1
    log.info("企业微信拉取 %d 条，入队 %d 条", len(items), n)
    return n


@app.post("/api/wecom/pull")
async def wecom_pull(_: None = Depends(require_token)) -> dict[str, Any]:
    """调试用：不等回调，手动拉一次。"""
    return {"ingested": await _pull_wecom()}


# ======================================================================
# 人工审核台
# ======================================================================
class SendRequest(BaseModel):
    draft_id: int
    text: Optional[str] = Field(default=None, max_length=800)


class ModeRequest(BaseModel):
    conversation_id: str
    mode: ConversationMode


class TakeoverRequest(BaseModel):
    conversation_id: str
    minutes: int = Field(default=30, ge=0, le=1440)


class ComposeRequest(BaseModel):
    """半自动模式：人工把商家消息粘进来，AI 出草稿。"""

    conversation_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    title: str = Field(default="", max_length=120)
    sender_name: str = Field(default="", max_length=60)
    is_group: bool = False


def _draft_view(row: Any) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "channel": row["conv_channel"] if "conv_channel" in keys else "",
        "conversation_title": row["conv_title"] if "conv_title" in keys else "",
        "status": row["status"],
        "action": row["action"],
        "intent": row["intent"],
        "reply": row["reply"],
        "reason": row["reason"],
        "send_result": row["send_result"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
        "evidence": json.loads(row["evidence"] or "[]"),
    }


class SwitchModelRequest(BaseModel):
    id: str = Field(min_length=1, max_length=80)


# ======================================================================
# 会话白名单管理（给不懂技术的人用：点一下就能加）
# ======================================================================
class ChatsRequest(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=200)


@app.get("/api/guard/chats")
async def guard_chats(_: None = Depends(require_token)) -> dict[str, Any]:
    """列出白名单 + 扫描微信会话列表（用于一键添加）。

    扫描只读会话名，不打开任何会话、不读任何聊天内容。
    """
    allowed = guard.allowed_chats()
    adapter = get_pipeline().adapter
    scanned: list[str] = []
    scan_error = ""
    if hasattr(adapter, "list_conversations"):
        try:
            scanned = await asyncio.to_thread(adapter.list_conversations)
        except Exception as exc:
            scan_error = f"{type(exc).__name__}: {str(exc)[:150]}"
    else:
        scan_error = f"当前通道（{settings.channel}）不支持扫描会话列表"

    return {
        "mode": guard.mode(),
        "allowed": allowed,
        "scanned": scanned,
        "scanned_not_allowed": [n for n in scanned if not guard.chat_allowed(n)],
        "scan_error": scan_error,
    }


@app.post("/api/guard/chats/add")
async def guard_chats_add(
    req: ChatsRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    added, skipped = [], []
    for name in req.names:
        name = (name or "").strip()
        if not name:
            continue
        (added if guard.add_chat(name) else skipped).append(name)
    log.info("白名单新增 %s 条：%s", len(added), added)
    return {"added": added, "skipped": skipped, "allowed": guard.allowed_chats()}


@app.post("/api/guard/chats/remove")
async def guard_chats_remove(
    req: ChatsRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    removed = [n for n in req.names if guard.drop_chat(n)]
    log.info("白名单移除 %s 条：%s", len(removed), removed)
    return {"removed": removed, "allowed": guard.allowed_chats()}


@app.get("/api/guard/audit")
async def guard_audit(
    limit: int = 40,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    path = guard.AUDIT
    items: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").strip().splitlines()[-limit:]:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    items.reverse()
    return {"items": items}


@app.get("/api/models")
async def list_models(_: None = Depends(require_token)) -> dict[str, Any]:
    return models.status()


@app.post("/api/models/switch")
async def switch_model(
    req: SwitchModelRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    """切换模型。切完**立即生效**，不用重启 —— settings.llm 是动态解析的。"""
    ok, msg = models.set_active(req.id)
    if ok:
        # 让正在跑的流水线换上新模型
        pipe = get_pipeline()
        from .llm import build_llm

        try:
            pipe.llm = build_llm()
            log.info("流水线已换用新模型：%s", settings.llm.model)
        except Exception:
            log.exception("换模型后重建客户端失败，下次调用会重试")
    return {"ok": ok, "message": msg, "active": (models.active_profile().to_dict()
                                                 if models.active_profile() else None)}


@app.get("/api/desk/queue")
async def desk_queue(_: None = Depends(require_token)) -> dict[str, Any]:
    return {"items": [_draft_view(r) for r in db.pending_drafts()]}


@app.post("/api/desk/compose")
async def desk_compose(
    req: ComposeRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    """半自动模式：人工录入一条商家消息，AI 出草稿，人工自己复制去发。

    永远不调用发送适配器 —— 发送动作在人手里。
    """
    return await get_pipeline().compose(
        conversation_id=req.conversation_id,
        text=req.text,
        title=req.title,
        sender_name=req.sender_name,
        is_group=req.is_group,
    )


@app.get("/api/desk/conversations")
async def desk_conversations(_: None = Depends(require_token)) -> dict[str, Any]:
    rows = db._conn().execute(
        "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "merchant_id": r["merchant_id"],
            "mode": r["mode"],
            "takeover_until": r["takeover_until"],
            "taken_over": db.is_human_taken_over(r["id"]),
            "version": r["version"],
            "updated_at": r["updated_at"],
        })
    return {"items": out}


@app.get("/api/desk/stats")
async def desk_stats(_: None = Depends(require_token)) -> dict[str, Any]:
    return {"stats": get_pipeline().stats, "queues": {
        k: len(v) for k, v in get_pipeline().buffers.items()
    }}


@app.post("/api/desk/send")
async def desk_send(
    req: SendRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    status = await get_pipeline().deliver_manual(req.draft_id, req.text)
    return {"status": status}


@app.post("/api/desk/discard")
async def desk_discard(
    draft_id: int = Body(..., embed=True),
    _: None = Depends(require_token),
) -> dict[str, Any]:
    db.update_draft(draft_id, status="discarded", reason="人工废弃")
    return {"status": "discarded"}


@app.post("/api/desk/mode")
async def desk_mode(
    req: ModeRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    """切换会话模式。权限由本接口的 Bearer Token 控制，绝不交给模型或聊天内容决定。"""
    db.set_mode(req.conversation_id, req.mode.value)
    return {"conversation_id": req.conversation_id, "mode": req.mode.value}


@app.post("/api/desk/takeover")
async def desk_takeover(
    req: TakeoverRequest,
    _: None = Depends(require_token),
) -> dict[str, Any]:
    if req.minutes <= 0:
        db.set_takeover(req.conversation_id, None)
        return {"conversation_id": req.conversation_id, "takeover_until": None}
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(minutes=req.minutes)).astimezone().isoformat(
        timespec="seconds"
    )
    db.set_takeover(req.conversation_id, until)
    return {"conversation_id": req.conversation_id, "takeover_until": until}


@app.get("/desk", response_class=HTMLResponse)
async def desk_page() -> HTMLResponse:
    if not DESK_HTML.exists():
        return HTMLResponse("<h1>desk.html 缺失</h1>", status_code=500)
    return HTMLResponse(DESK_HTML.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
