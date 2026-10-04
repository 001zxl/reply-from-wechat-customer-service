"""集中读取环境变量。任何密钥都只从环境变量来，不落仓库。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_allow_empty(name: str, default: str = "") -> str:
    """和 _env 的区别：显式设成空字符串时返回空，而不是回退到默认值。

    有些开关（比如夜间静默）需要"显式设空 = 关掉"的语义，
    用 _env 的话空字符串会被当成"没设置"，用户想关也关不掉。
    """
    if name in os.environ:
        return os.environ[name].strip()
    return default


@dataclass(frozen=True)
class WeComConfig:
    corp_id: str = field(default_factory=lambda: _env("WECOM_CORP_ID"))
    kf_secret: str = field(default_factory=lambda: _env("WECOM_KF_SECRET"))
    callback_token: str = field(default_factory=lambda: _env("WECOM_CALLBACK_TOKEN"))
    encoding_aes_key: str = field(default_factory=lambda: _env("WECOM_ENCODING_AES_KEY"))

    @property
    def ready(self) -> bool:
        return all([self.corp_id, self.kf_secret, self.callback_token, self.encoding_aes_key])


@dataclass(frozen=True)
class LLMConfig:
    """当前生效的模型参数。

    注意：这些值现在来自 config/models.json 里 active 指定的那个档案，
    不再直接从 .env 读。要换模型改 models.json，或者用 /desk 上的下拉框。
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 2500
    backend: str = field(default_factory=lambda: _env("LLM_BACKEND", "deepseek").lower())
    timeout: float = 45.0
    max_tool_rounds: int = 4
    profile_id: str = ""
    profile_label: str = ""

    @property
    def provider_label(self) -> str:
        if "deepseek.com" in self.base_url:
            return "deepseek"
        if "dashscope" in self.base_url:
            return "阿里云百炼"
        return self.profile_label or self.base_url or "（未配置）"


@dataclass(frozen=True)
class LogisticsConfig:
    provider: str = field(default_factory=lambda: _env("LOGISTICS_PROVIDER", "mock").lower())
    kd100_customer: str = field(default_factory=lambda: _env("KD100_CUSTOMER"))
    kd100_key: str = field(default_factory=lambda: _env("KD100_KEY"))
    kd100_com: str = field(default_factory=lambda: _env("KD100_COM", "shentong"))
    sto_appkey: str = field(default_factory=lambda: _env("STO_APPKEY"))
    sto_secret: str = field(default_factory=lambda: _env("STO_SECRET"))
    sto_api_base: str = field(default_factory=lambda: _env("STO_API_BASE"))


@dataclass(frozen=True)
class Settings:
    app_token: str = field(default_factory=lambda: _env("APP_TOKEN"))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8787") or 8787))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/assistant.db"))
    channel: str = field(default_factory=lambda: _env("WECHAT_CHANNEL", "mock").lower())
    # macOS 截屏+OCR 通道要监听的会话名，逗号分隔。留空表示只发不收。
    macos_watch: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip() for x in _env("WECHAT_MAC_WATCH").split(",") if x.strip()
        )
    )
    auto_reply_max_per_minute: int = field(
        default_factory=lambda: int(_env("AUTO_REPLY_MAX_PER_MINUTE", "6") or 6)
    )
    min_reply_interval_seconds: float = field(
        default_factory=lambda: float(_env("MIN_REPLY_INTERVAL_SECONDS", "3") or 3)
    )
    # ============ 风控（降低被平台判定为机器行为的概率）============

    # 1. 夜间静默：这段时间内不自动发送，只出草稿。空字符串 = 不启用。
    #    支持跨午夜，例 "22:00-08:00"
    quiet_hours: str = field(
        default_factory=lambda: _env_allow_empty("QUIET_HOURS", "22:00-08:00")
    )

    # 2. 每日自动发送上限（按会话计）
    auto_reply_max_per_day: int = field(
        default_factory=lambda: int(_env("AUTO_REPLY_MAX_PER_DAY", "150") or 150)
    )

    # 3. 随机延迟：发送前等一个区间内的随机时长，避免固定节奏
    reply_delay_min: float = field(
        default_factory=lambda: float(_env("REPLY_DELAY_MIN", "1.5") or 1.5)
    )
    reply_delay_max: float = field(
        default_factory=lambda: float(_env("REPLY_DELAY_MAX", "5.0") or 5.0)
    )

    # 4. 相似度检测：新回复和最近 N 条太像就不自动发（防群发特征）
    similar_reply_window: int = field(
        default_factory=lambda: int(_env("SIMILAR_REPLY_WINDOW", "5") or 5)
    )
    similar_reply_threshold: float = field(
        default_factory=lambda: float(_env("SIMILAR_REPLY_THRESHOLD", "0.88") or 0.88)
    )

    # 5. 异常熔断：连续失败 N 次就暂停该会话一段时间
    circuit_breaker_failures: int = field(
        default_factory=lambda: int(_env("CIRCUIT_BREAKER_FAILURES", "3") or 3)
    )
    circuit_breaker_cooldown_minutes: int = field(
        default_factory=lambda: int(_env("CIRCUIT_BREAKER_COOLDOWN_MINUTES", "30") or 30)
    )

    # 6. 打字节奏：粘贴完成到点发送之间停一下，停多久跟字数相关
    typing_simulation: bool = field(
        default_factory=lambda: _env("TYPING_SIMULATION", "1") not in ("0", "false", "no")
    )

    # 截屏类通道的轮询间隔（秒）。OCR 一次约 0.6s，别设太小。
    poll_interval: float = field(
        default_factory=lambda: float(_env("POLL_INTERVAL_SECONDS", "4") or 4)
    )
    # 【最重要的安全闸】发送白名单。**默认为空 = 任何消息都不许发出去。**
    # 只有把会话名写进 WECHAT_SEND_ALLOWLIST，那个会话才允许被发送。
    # 这是硬约束，不依赖 DRY_RUN，也不依赖调用方自觉。
    send_allowlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip() for x in _env("WECHAT_SEND_ALLOWLIST").split(",") if x.strip()
        )
    )
    # 演练模式：只读消息、只出草稿，绝不发送。第一次接真实微信时务必打开。
    dry_run: bool = field(
        default_factory=lambda: _env("DRY_RUN", "1") not in ("0", "false", "no", "")
    )
    logistics: LogisticsConfig = field(default_factory=LogisticsConfig)
    wecom: WeComConfig = field(default_factory=WeComConfig)

    @property
    def llm(self) -> LLMConfig:
        """每次访问都重新解析 —— 这样在 /desk 上切了模型立刻生效，不用重启。"""
        from . import models

        cfg = models.active_llm_config()
        return LLMConfig(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
            profile_id=cfg["profile_id"],
            profile_label=cfg["profile_label"],
        )

    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (ROOT / p)


settings = Settings()
CHATS_CONFIG = ROOT / "config" / "chats.json"
KNOWLEDGE_FILE = ROOT / "config" / "knowledge.md"


def load_allowed_chats() -> dict:
    """允许接入的会话白名单。默认拒绝一切未登记会话。

    chats.json 结构：
    {
      "global": {"default_mode": "review"},
      "conversations": [
        {"channel": "wecom_kf", "channel_chat_id": "wkXXXX|wmYYYY",
         "title": "杭州XX电商-客服小李", "merchant_id": "m-001", "mode": "review"}
      ]
    }
    """
    if not CHATS_CONFIG.exists():
        return {"global": {"default_mode": "off"}, "conversations": []}
    return json.loads(CHATS_CONFIG.read_text(encoding="utf-8"))


def load_knowledge() -> str:
    """网点已确认的业务知识（时效、价格、赔付口径）。没有就返回空，模型必须承认不知道。"""
    if not KNOWLEDGE_FILE.exists():
        return ""
    text = KNOWLEDGE_FILE.read_text(encoding="utf-8").strip()
    if text.startswith("<!--") and "还没有" in text[:400]:
        return ""
    return text
