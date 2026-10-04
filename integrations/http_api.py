"""配置驱动的 HTTP 接口集成。

网点的 ERP / 申通开放平台大多是 REST。与其每个接口写一遍代码，
不如在 config/integrations.json 里描述清楚，这里统一执行。

关键约束：
- 密钥只从环境变量取（配置里写 env 名字，不写值）
- write 类动作这里**拒绝执行**，由上层转成待办交人工
- 超时、HTTP 错误一律返回 ok=False，让模型走"查不到"的分支，而不是猜
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

import httpx

from .base import ActionResult, ActionSpec


class HttpIntegration:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.system = str(cfg.get("id") or "")
        self.label = str(cfg.get("label") or self.system)
        self.description = str(cfg.get("description") or "")
        self.base_url = str(cfg.get("base_url") or "").rstrip("/")
        self._specs: dict[str, ActionSpec] = {}
        for a in cfg.get("actions", []):
            spec = ActionSpec(
                system=self.system,
                name=str(a.get("name")),
                label=str(a.get("label") or a.get("name")),
                risk=a.get("risk", "read"),
                when=str(a.get("when") or ""),
                params=sorted({
                    m for key, val in (a.get("params") or {}).items()
                    for m in PLACEHOLDER.findall(str(val))
                } | set((a.get("params") or {}).keys()) - {"env"}),
                timeout=float(a.get("timeout", 15)),
            )
            self._specs[spec.name] = spec
            # 保留原始配置（path/method 等）
            setattr(spec, "_raw", a)

    def actions(self) -> list[ActionSpec]:
        return list(self._specs.values())

    def spec(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def _headers(self) -> dict[str, str]:
        auth = self.cfg.get("auth") or {}
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth.get("type") == "header":
            env_name = auth.get("env", "")
            token = os.environ.get(env_name, "")
            if token:
                headers[str(auth.get("header", "Authorization"))] = (
                    f"{auth.get('prefix', '')}{token}"
                )
        return headers

    async def run(self, action: str, args: dict[str, Any]) -> ActionResult:
        spec = self._specs.get(action)
        if spec is None:
            return ActionResult(False, source=self.system,
                                error=f"{self.system} 没有名为 {action} 的动作")
        if spec.risk == "write":
            return ActionResult(
                False, source=self.system,
                error=f"「{spec.label}」是会产生真实副作用的操作，本系统不代执行，已转人工",
            )

        raw = getattr(spec, "_raw", {})
        method = str(raw.get("method", "GET")).upper()
        path = str(raw.get("path", ""))
        url = self.base_url + path
        # 参数模板替换，例如 {"waybill": "{waybill_no}"}
        filled = {
            k: (str(v).format(**args) if isinstance(v, str) else v)
            for k, v in (raw.get("params") or {}).items()
        }
        body = {k: v for k, v in (raw.get("body") or {}).items()} if raw.get("body") else None

        try:
            async with httpx.AsyncClient(timeout=spec.timeout) as cli:
                if method == "GET":
                    resp = await cli.get(url, params=filled, headers=self._headers())
                else:
                    payload = body if body is not None else filled
                    resp = await cli.request(method, url, json=payload, headers=self._headers())
            data = resp.json()
        except Exception as exc:
            return ActionResult(False, source=self.system,
                                error=f"{self.label} 请求失败：{type(exc).__name__}")

        if resp.status_code >= 400:
            return ActionResult(False, source=self.system,
                                error=f"{self.label} 返回 HTTP {resp.status_code}")

        text = raw.get("reply_template") or ""
        if text:
            try:
                text = text.format(**{**args, "data": json.dumps(data, ensure_ascii=False)})
            except (KeyError, IndexError):
                text = ""
        if not text:
            text = f"{self.label} 返回：{json.dumps(data, ensure_ascii=False)[:600]}"

        return ActionResult(True, text=f"【{self.label}】{text}", data=data, source=self.system)
