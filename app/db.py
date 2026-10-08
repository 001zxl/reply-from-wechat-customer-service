"""SQLite 持久化。第一版单网点够用；要上多网点把 DB_PATH 换成 Postgres 即可。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS runtime_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
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


def label_speaker(direction: str, text: str, sender_name: str, is_group: bool) -> str:
    """群聊里给正文带上发言人。

    ★ 为什么必须有（外部审查 P1）：群里客服甲发单号、客服乙说"这个退回来"，
      如果历史只剩两段裸正文，模型只能**猜**"这个"是甲的哪一票 ——
      猜错就是把乙的诉求登记到甲的运单上，是真会办错事的。
      私聊不加前缀：来回只有两个人，加了只是噪音。
    """
    name = (sender_name or "").strip()
    if is_group and direction == "in" and name:
        return f"{name}：{text}"
    return text


def history_before(conv_id: str, before_id: int, limit: int = 20) -> list[dict[str, str]]:
    """批处理开始之前的历史，不含本批消息，避免和 batch_text 重复。

    群聊会带上发言人（label_speaker）。
    """
    rows = _conn().execute(
        """SELECT direction, text, sender_name, is_group FROM messages
            WHERE conversation_id=? AND id < ? ORDER BY id DESC LIMIT ?""",
        (conv_id, before_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        role = "user" if r["direction"] == "in" else "assistant"
        out.append({"role": role, "content": label_speaker(
            r["direction"], r["text"], r["sender_name"], bool(r["is_group"]))})
    return out


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recent_incoming_from(conv_id: str, sender_id: str,
                         window_seconds: int = 180, limit: int = 6,
                         exclude_id: Optional[int] = None) -> list[str]:
    """同一会话、同一成员、最近 window_seconds 内发过的入站正文（旧的在前）。

    ★ 用来判断"这句是不是在接着上一句补充"（外部审查 P2）。群里很常见：
        甲：773123456789012
        甲：这票不要了退回来          ← 没单号
    第二句要是因为"没被点名又没单号"被丢掉，等于只处理了一半，
    而且是**把上下文丢了**，模型看到的批次是残缺的。
    范围卡得很死：同会话 + 同成员 + 短窗口，不会因此放开全群闲聊。
    """
    rows = _conn().execute(
        """SELECT id, text, received_at FROM messages
            WHERE conversation_id=? AND direction='in' AND sender_id=?
            ORDER BY id DESC LIMIT ?""",
        (conv_id, sender_id, max(1, limit)),
    ).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, window_seconds))
    out: list[str] = []
    for r in reversed(rows):
        if exclude_id is not None and int(r["id"]) == int(exclude_id):
            continue
        dt = _parse_iso(r["received_at"] or "")
        if dt is not None and dt < cutoff:
            continue          # 时间解析得出来才卡窗口；空时间戳当作"刚发生"
        out.append(r["text"] or "")
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
        """SELECT direction, text, sender_name, is_group FROM messages
            WHERE conversation_id=? ORDER BY id DESC LIMIT ?""",
        (conv_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        role = "user" if r["direction"] == "in" else "assistant"
        out.append({"role": role, "content": label_speaker(
            r["direction"], r["text"], r["sender_name"], bool(r["is_group"]))})
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


# 草稿允许被发送入口占用的状态。
# ★ 关键：`sending` 和 `unknown` **不在**里面。
#   · sending = 已经有人在发（或进程崩在这个状态），再发就是重复发送
#   · unknown = 点了发送但回读没确认，**可能已经发出去了**，绝不能重发
#   这两种要走人工核实，不能从普通发送入口再来一次（外部审查 P1）。
SENDABLE_FROM: tuple[str, ...] = ("draft", "approved", "blocked", "failed")


def claim_draft(draft_id: int,
                allowed_from: tuple[str, ...] = SENDABLE_FROM,
                to: str = "sending") -> bool:
    """原子占用草稿：只有当前状态在 allowed_from 里，才改成 to。

    返回 True = 抢到了，可以发；False = 别人抢走了 / 状态不允许发。

    为什么必须是**一条带 WHERE 的 UPDATE**（外部审查 P1）：
      "先 get_draft 看状态，再决定发不发"一定有竞态 —— 两个审核请求
      同时进来，都读到 status='draft'，然后都发，同一条草稿发两遍。
      实测就是这样（还顺带把 messages.event_id 的 UNIQUE 约束撞了）。
      放进一条 UPDATE，由 SQLite 保证只有一个 rowcount=1。
    """
    if not allowed_from:
        return False
    marks = ",".join("?" for _ in allowed_from)
    with tx() as c:
        cur = c.execute(
            f"UPDATE outbox SET status=?, updated_at=? "
            f"WHERE id=? AND status IN ({marks})",
            (to, now_iso(), draft_id, *allowed_from),
        )
        return cur.rowcount == 1


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


# ---------------- 运行时状态（熔断 / 暂停）----------------

def state_get(key: str, default: str = "") -> str:
    row = _conn().execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def state_set(key: str, value: str) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO runtime_state(key, value, updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now_iso()),
        )


def state_del(key: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM runtime_state WHERE key=?", (key,))


# ---------------- 风控用的计数与查询 ----------------

def sent_count_today(conv_id: str) -> int:
    """今天这个会话自动发了多少条（用于每日上限）。"""
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    rows = _conn().execute(
        "SELECT sent_at FROM outbox WHERE conversation_id=? AND status='sent' AND sent_at IS NOT NULL",
        (conv_id,),
    ).fetchall()
    n = 0
    for r in rows:
        try:
            if datetime.fromisoformat(r["sent_at"]).astimezone().strftime("%Y-%m-%d") == today:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def recent_sent_replies(conv_id: str, limit: int = 5) -> list[str]:
    """最近自动发出去的几条回复内容（用于相似度检测）。"""
    rows = _conn().execute(
        """SELECT reply FROM outbox
            WHERE conversation_id=? AND status='sent' AND reply != ''
            ORDER BY id DESC LIMIT ?""",
        (conv_id, limit),
    ).fetchall()
    return [r["reply"] for r in rows]


def recent_sent_all(minutes: int = 10, limit: int = 80) -> list[tuple[str, str]]:
    """最近 N 分钟内所有会话发出去的 (会话, 内容)。用于跨会话群发检测。"""
    cutoff = datetime.now(timezone.utc).timestamp() - minutes * 60
    rows = _conn().execute(
        """SELECT conversation_id, reply, sent_at FROM outbox
            WHERE status='sent' AND reply != '' AND sent_at IS NOT NULL
            ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            if datetime.fromisoformat(r["sent_at"]).timestamp() >= cutoff:
                out.append((r["conversation_id"], r["reply"]))
        except (TypeError, ValueError):
            continue
    return out


def consecutive_failures(conv_id: str) -> int:
    raw = state_get(f"cb:{conv_id}")
    if not raw:
        return 0
    try:
        return int(json.loads(raw).get("failures", 0))
    except (json.JSONDecodeError, ValueError, AttributeError):
        return 0


def record_send_outcome(conv_id: str, ok: bool) -> int:
    """记录一次发送结果，返回当前连续失败次数。"""
    n = 0 if ok else consecutive_failures(conv_id) + 1
    state_set(f"cb:{conv_id}", json.dumps({"failures": n, "at": now_iso()}))
    return n


def pause_conversation(conv_id: str, minutes: int, reason: str) -> str:
    """熔断：暂停这个会话的自动发送一段时间。"""
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).astimezone()
    state_set(f"paused:{conv_id}", json.dumps(
        {"until": until.isoformat(timespec="seconds"), "reason": reason[:200]},
        ensure_ascii=False,
    ))
    return until.isoformat(timespec="seconds")


def paused_until(conv_id: str) -> Optional[str]:
    raw = state_get(f"paused:{conv_id}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        until = data.get("until", "")
        if not until:
            return None
        dt = datetime.fromisoformat(until)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= dt:
            state_del(f"paused:{conv_id}")     # 到期自动解除
            return None
        return until
    except (json.JSONDecodeError, ValueError):
        return None


def clear_pause(conv_id: str) -> None:
    state_del(f"paused:{conv_id}")
    state_del(f"cb:{conv_id}")


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
