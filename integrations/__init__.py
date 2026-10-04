"""外部系统集成的加载入口。

配置在 config/integrations.json。**默认不配任何系统** —— 也就是默认状态下
模型除了查物流和查知识库，没有别的外部能力。要用哪个再往里加。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import ROOT

from .base import ActionResult, ActionSpec, Integration

log = logging.getLogger("integrations")

CONFIG_FILE = ROOT / "config" / "integrations.json"


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {"systems": []}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("integrations.json 解析失败：%s", exc)
        return {"systems": []}


def build_integrations() -> list[Integration]:
    out: list[Integration] = []
    for cfg in load_config().get("systems", []):
        if not cfg.get("enabled", True):
            continue
        kind = cfg.get("kind")
        try:
            if kind == "http":
                from .http_api import HttpIntegration

                out.append(HttpIntegration(cfg))
            elif kind == "command":
                from .local_cmd import CommandIntegration

                out.append(CommandIntegration(cfg))
            else:
                log.warning("未知的集成类型：%s（%s）", kind, cfg.get("id"))
        except Exception:
            log.exception("加载集成 %s 失败", cfg.get("id"))
    return out


__all__ = [
    "ActionResult", "ActionSpec", "Integration",
    "build_integrations", "load_config", "CONFIG_FILE",
]
