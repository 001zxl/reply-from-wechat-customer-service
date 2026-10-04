"""模型配置：多模型档案 + 运行时切换。

交付场景的核心需求：**对方不改代码就能换模型。**
所以配置放在 config/models.json，密钥放 .env，两边分开：

  config/models.json   写"有哪些模型、接口地址是什么、密钥读哪个环境变量"
  .env                 写"密钥的值"

这样 config/models.json 可以安全地随项目交付（里面没有密钥）。

切换方式（三种，任选）：
  1. 改 config/models.json 里的 "active"
  2. 命令行：.venv/bin/python bridge/switch_model.py use deepseek-pro
  3. 网页：/desk 侧栏的模型下拉框
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

log = logging.getLogger("models")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "models.json"

# 自己也加载一次 .env —— 命令行工具可能只 import 了这个模块，
# 不走 app/config.py，那样就读不到密钥。
load_dotenv(ROOT / ".env")

# 出现这些字样就认为是没填，不算配置好
PLACEHOLDER = re.compile(r"在这里填|填你的|xxx|your[_-]?key|replace|todo", re.I)


@dataclass
class ModelProfile:
    id: str
    label: str
    base_url: str
    model: str
    api_key_env: str
    note: str = ""
    enabled: bool = True
    temperature: float = 0.3
    max_tokens: int = 2500
    api_key: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        """密钥有没有、地址和模型名填没填。"""
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "enabled": self.enabled,
            "configured": self.configured,
            "problems": self.problems,
        }


def _read_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {"active": "", "profiles": []}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("config/models.json 解析失败：%s", exc)
        return {"active": "", "profiles": []}


def _write_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


def profiles(include_disabled: bool = False) -> list[ModelProfile]:
    out: list[ModelProfile] = []
    for raw in _read_config().get("profiles", []):
        if not raw.get("enabled", True) and not include_disabled:
            continue
        env_name = str(raw.get("api_key_env") or "")
        key = (os.environ.get(env_name) or "").strip()
        base_url = str(raw.get("base_url") or "").strip()
        model = str(raw.get("model") or "").strip()

        problems: list[str] = []
        if not env_name:
            problems.append("没配 api_key_env")
        elif not key:
            problems.append(f"环境变量 {env_name} 是空的")
        elif PLACEHOLDER.search(key):
            problems.append(f"{env_name} 还是占位符，没填真实密钥")
        if not base_url:
            problems.append("没填 base_url")
        if not model:
            problems.append("没填 model 名")

        out.append(ModelProfile(
            id=str(raw.get("id") or ""),
            label=str(raw.get("label") or raw.get("id") or ""),
            base_url=base_url,
            model=model,
            api_key_env=env_name,
            note=str(raw.get("note") or ""),
            enabled=bool(raw.get("enabled", True)),
            temperature=float(raw.get("temperature", 0.3)),
            max_tokens=int(raw.get("max_tokens", 2500)),
            api_key=key,
            problems=problems,
        ))
    return out


def get(profile_id: str) -> Optional[ModelProfile]:
    for p in profiles(include_disabled=True):
        if p.id == profile_id:
            return p
    return None


def active_id() -> str:
    return str(_read_config().get("active") or "").strip()


def active_profile() -> Optional[ModelProfile]:
    """当前生效的模型。active 没配或那个模型不可用时，自动退到第一个可用的。"""
    pid = active_id()
    if pid:
        p = get(pid)
        if p and p.configured:
            return p
        if p:
            log.warning("当前模型 %s 不可用：%s", pid, "；".join(p.problems))
    for p in profiles():
        if p.configured:
            if pid and p.id != pid:
                log.warning("自动回退到可用模型：%s", p.id)
            return p
    return None


def set_active(profile_id: str) -> tuple[bool, str]:
    """切换当前模型。返回 (是否成功, 说明)。"""
    p = get(profile_id)
    if p is None:
        return False, f"没有这个模型：{profile_id}"
    if not p.configured:
        return False, f"{p.label} 还不能用：{'；'.join(p.problems)}"
    cfg = _read_config()
    cfg["active"] = profile_id
    _write_config(cfg)
    log.info("已切换模型：%s（%s）", p.label, p.model)
    return True, f"已切换到 {p.label}（{p.model}）"


def active_llm_config() -> dict[str, Any]:
    """给 app/config.py 和 app/llm.py 用的当前模型参数。"""
    p = active_profile()
    if p is None:
        return {
            "api_key": "", "base_url": "", "model": "",
            "temperature": 0.3, "max_tokens": 2500,
            "profile_id": "", "profile_label": "（没有可用模型）",
        }
    return {
        "api_key": p.api_key,
        "base_url": p.base_url,
        "model": p.model,
        "temperature": p.temperature,
        "max_tokens": p.max_tokens,
        "profile_id": p.id,
        "profile_label": p.label,
    }


def status() -> dict[str, Any]:
    """给 /health 和界面用的体检结果。"""
    act = active_profile()
    return {
        "active": act.to_dict() if act else None,
        "configured": [p.id for p in profiles() if p.configured],
        "not_configured": [
            {"id": p.id, "label": p.label, "problems": p.problems}
            for p in profiles() if not p.configured
        ],
        "available": [p.to_dict() for p in profiles()],
    }
