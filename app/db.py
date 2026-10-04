"""SQLite 持久化。第一版单网点够用；要上多网点把 DB_PATH 换成 Postgres 即可。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings
from .schemas import ConversationMode, IncomingMessage, now_iso

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS conversations (
  id                TEXT PRIMARY KEY,
  channel           TEXT NOT NULL,
  channel_chat_id   TEXT NOT NULL,
  title             TEXT NOT NULL DEFAULT '',
  merchant_id       TEXT NOT NULL DEFAULT '',
  mode              TEXT NOT NULL DEFAULT 'review',
  takeover_until    TEXT,
  version           INTEGER NOT NULL DEFAULT 0,
  last_auto_sent_at TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE(channel, channel_chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id        TEXT NOT NULL UNIQUE,
  conversation_id TEXT NOT NULL,
  sender_id       TEXT NOT NULL,
  sender_name     TEXT NOT NULL DEFAULT '',
  direction       TEXT NOT NULL,             -- in | out
  text            TEXT NOT NULL,
  is_group        INTEGER NOT NULL DEFAULT 0,
  mentioned_bot   INTEGER NOT NULL DEFAULT 0,
  received_at     TEXT NOT NULL,
  batch_id        TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  batch_id        TEXT NOT NULL DEFAULT '',
  action          TEXT NOT NULL,
  intent          TEXT NOT NULL DEFAULT '',
  reply           TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL,             -- draft|approved|sending|sent|failed|unknown|discarded|blocked
  reason          TEXT NOT NULL DEFAULT '',
  evidence        TEXT NOT NULL DEFAULT '[]',
  send_result     TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, id);

CREATE TABLE IF NOT EXISTS cases (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  waybill_no      TEXT NOT NULL DEFAULT '',
  intent          TEXT NOT NULL DEFAULT '',
  summary         TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'open',
  owner           TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id   TEXT NOT NULL DEFAULT '',
  batch_id          TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  ok                INTEGER NOT NULL DEFAULT 0,
  tool_rounds       INTEGER NOT NULL DEFAULT 0,
  latency_ms        INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  error             TEXT NOT NULL DEFAULT '',
  created_at        TEXT NOT NULL
);
"""

_local = threading.local()


def _conn() -> sqlite3.Connection:
    path: Path = settings.db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = _conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    conn = _conn()
    conn.executescript(SCHEMA)


# ---------------- conversations ----------------

def upsert_conversation(
    conv_id: str,
    channel: str,
    channel_chat_id: str,
    title: str = "",
    merchant_id: str = "",
    mode: str = ConversationMode.review.value,
) -> None:
    ts = now_iso()
    with tx() as c:
        c.execute(
            """INSERT INTO conversations
                 (id, channel, channel_chat_id, title, merchant_id, mode, version, created_at, updated_at)
               VALUES (?,?,?,?,?,?,0,?,?)
               ON CONFLICT(channel, channel_chat_id) DO UPDATE SET
                 title=excluded.title, updated_at=excluded.updated_at""",
            (conv_id, channel, channel_chat_id, title, merchant_id, mode, ts, ts),
        )


def get_conversation(conv_id: str) -> Optional[sqlite3.Row]:
    return _conn().execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()


def get_conversation_by_chat(channel: str, channel_chat_id: str) -> Optional[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM conversations WHERE channel=? AND channel_chat_id=?",
        (channel, channel_chat_id),
    ).fetchone()


def set_conversation_title(conv_id: str, title: str) -> None:
    """单独改标题。不要把 upsert_conversation 用在已存在的行上——
    它的 ON CONFLICT 目标是 (channel, channel_chat_id)，和 id 主键是两回事。"""
    with tx() as c:
        c.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
            (title, now_iso(), conv_id),
        )


def set_mode(conv_id: str, mode: str) -> None:
    with tx() as c:
        c.execute(
            "UPDATE conversations SET mode=?, updated_at=? WHERE id=?",
            (mode, now_iso(), conv_id),
        )


def set_takeover(conv_id: str, until: Optional[str]) -> None:
    """人工接管。until=None 表示取消接管。接管期间 AI 一律不发送。"""
    with tx() as c:
        c.execute(
            "UPDATE conversations SET takeover_until=?, updated_at=? WHERE id=?",
            (until, now_iso(), conv_id),
        )


def bump_version(conv_id: str) -> int:
    """每条入站消息都让版本 +1。模型思考期间来了新消息，旧草稿作废。"""
    with tx() as c:
        c.execute(
            "UPDATE conversations SET version=version+1, updated_at=? WHERE id=?",
            (now_iso(), conv_id),
        )
        row = c.execute("SELECT version FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return int(row["version"]) if row else 0


def version_of(conv_id: str) -> int:
    row = get_conversation(conv_id)
    return int(row["version"]) if row else 0


def is_human_taken_over(conv_id: str, at: Optional[str] = None) -> bool:
    row = get_conversation(conv_id)
    if not row or not row["takeover_until"]:
        return False
    try:
        until = datetime.fromisoformat(row["takeover_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        current = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    except ValueError:
        return True          # 解析不了就当作接管中，保守处理
    return current < until


# ---------------- messages ----------------

def save_incoming(msg: IncomingMessage, batch_id: str = "") -> Optional[int]:
    """写入入站消息。event_id 冲突说明重复回调，返回 None。"""
    try:
        with tx() as c:
            cur = c.execute(
                """INSERT INTO messages
                     (event_id, conversation_id, sender_id, sender_name, direction, text,
                      is_group, mentioned_bot, received_at, batch_id, created_at)
                   VALUES (?,?,?,?, 'in', ?,?,?,?,?,?)""",
                (
                    msg.event_id, msg.conversation_id, msg.sender_id, msg.sender_name,
                    msg.text, int(msg.is_group), int(msg.mentioned_bot),
                    msg.received_at, batch_id, now_iso(),
                ),
            )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def history_before(conv_id: str, before_id: int, limit: int = 20) -> list[dict[str, str]]:
    """批处理开始之前的历史，不含本批消息，避免和 batch_text 重复。"""
    rows = _conn().execute(
        """SELECT direction, text FROM messages
            WHERE conversation_id=? AND id < ? ORDER BY id DESC LIMIT ?""",
        (conv_id, before_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        role = "user" if r["direction"] == "in" else "assistant"
        out.append({"role": role, "content": r["text"]})
    return out


def save_outgoing(conv_id: str, text: str, sender_id: str = "assistant") -> None:
    """只有真正发送成功的消息才写入，避免未发出的草稿污染上下文。"""
    with tx() as c:
        c.execute(
            """INSERT INTO messages
                 (event_id, conversation_id, sender_id, sender_name, direction, text,
                  is_group, mentioned_bot, received_at, created_at)
               VALUES (?,?,?,?, 'out', ?, 0,0,?,?)""",
            (f"out-{conv_id}-{now_iso()}-{abs(hash(text)) % 10**8}", conv_id, sender_id, "助手",
             text, now_iso(), now_iso()),
        )


def recent_history(conv_id: str, limit: int = 20) -> list[dict[str, str]]:
    rows = _conn().execute(
        """SELECT direction, text, sender_name FROM messages
            WHERE conversation_id=? ORDER BY id DESC LIMIT ?""",
        (conv_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        if r["direction"] == "in":
            out.append({"role": "user", "content": r["text"]})
        else:
            out.append({"role": "assistant", "content": r["text"]})
    return out


# ---------------- outbox ----------------

def create_draft(
    conv_id: str, batch_id: str, action: str, intent: str, reply: str,
    evidence: list[dict[str, Any]], status: str, reason: str = "",
) -> int:
    ts = now_iso()
    with tx() as c:
        cur = c.execute(
            """INSERT INTO outbox
                 (conversation_id, batch_id, action, intent, reply, status, reason,
                  evidence, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (conv_id, batch_id, action, intent, reply, status, reason,
             json.dumps(evidence, ensure_ascii=False), ts, ts),
        )
        return int(cur.lastrowid)


def get_draft(draft_id: int) -> Optional[sqlite3.Row]:
    return _conn().execute("SELECT * FROM outbox WHERE id=?", (draft_id,)).fetchone()


def update_draft(draft_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE outbox SET {cols} WHERE id=?", (*fields.values(), draft_id))


def pending_drafts(limit: int = 100) -> list[sqlite3.Row]:
    """待处理草稿，带上会话渠道和标题，审核台要用来区分自动/半自动。"""
    return _conn().execute(
        """SELECT o.*, c.channel AS conv_channel, c.title AS conv_title, c.mode AS conv_mode
             FROM outbox o
             LEFT JOIN conversations c ON c.id = o.conversation_id
            WHERE o.status IN ('draft','approved','unknown','failed','blocked')
            ORDER BY o.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def last_auto_sent_at(conv_id: str) -> Optional[str]:
    row = _conn().execute(
        "SELECT last_auto_sent_at FROM conversations WHERE id=?", (conv_id,)
    ).fetchone()
    return row["last_auto_sent_at"] if row else None


def mark_auto_sent(conv_id: str) -> None:
    with tx() as c:
        c.execute(
            "UPDATE conversations SET last_auto_sent_at=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), conv_id),
        )


def auto_sent_count_last_minute(conv_id: str) -> int:
    """sent_at 是带时区的本地 ISO 字符串，不能用 SQLite 的 datetime() 直接比。"""
    rows = _conn().execute(
        "SELECT sent_at FROM outbox WHERE conversation_id=? AND status='sent' AND sent_at IS NOT NULL",
        (conv_id,),
    ).fetchall()
    cutoff = datetime.now(timezone.utc).timestamp() - 60
    n = 0
    for r in rows:
        try:
            if datetime.fromisoformat(r["sent_at"]).timestamp() >= cutoff:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def record_model_call(
    conv_id: str, batch_id: str, model: str, ok: bool, tool_rounds: int,
    latency_ms: int, prompt_tokens: int = 0, completion_tokens: int = 0, error: str = "",
) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO model_calls
                 (conversation_id, batch_id, model, ok, tool_rounds, latency_ms,
                  prompt_tokens, completion_tokens, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (conv_id, batch_id, model, int(ok), tool_rounds, latency_ms,
             prompt_tokens, completion_tokens, error[:500], now_iso()),
        )


# ---------------- cases ----------------

def upsert_case(conv_id: str, waybill_no: str, intent: str, summary: str) -> int:
    ts = now_iso()
    conn = _conn()
    row = conn.execute(
        "SELECT id FROM cases WHERE conversation_id=? AND waybill_no=? AND status='open'",
        (conv_id, waybill_no),
    ).fetchone()
    if row:
        with tx() as c:
            c.execute(
                "UPDATE cases SET intent=?, summary=?, updated_at=? WHERE id=?",
                (intent, summary, ts, row["id"]),
            )
        return int(row["id"])
    with tx() as c:
        cur = c.execute(
            """INSERT INTO cases (conversation_id, waybill_no, intent, summary, status, created_at, updated_at)
               VALUES (?,?,?,?, 'open', ?,?)""",
            (conv_id, waybill_no, intent, summary, ts, ts),
        )
        return int(cur.lastrowid)


def open_cases(conv_id: str, limit: int = 5) -> list[sqlite3.Row]:
    return _conn().execute(
        "SELECT * FROM cases WHERE conversation_id=? AND status='open' ORDER BY id DESC LIMIT ?",
        (conv_id, limit),
    ).fetchall()
