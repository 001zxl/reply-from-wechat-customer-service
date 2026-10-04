"""申通开放平台适配器 —— 骨架，需要按你实际拿到的接口文档填实现。

为什么这里不写死实现：
申通开放平台（open.sto.cn）的接口路径、签名算法、字段名随合作类型（月结客户 /
网点 / ISV）不同，且需要商务开通后才给文档。凭猜测写出来的接口只会给出
"看起来查到了但其实全是错"的结果，比查不到更危险。

所以这里的设计是：未配置或未实现时，显式返回 ok=False，让流程转人工。
等你拿到文档，只需实现 `_call_api()`，其余归一化逻辑已经写好。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from app.schemas import LogisticsEvent, LogisticsResult
from app.config import settings


class StoOpenProvider:
    name = "sto"

    def __init__(self) -> None:
        cfg = settings.logistics
        self.appkey = cfg.sto_appkey
        self.secret = cfg.sto_secret
        self.base = cfg.sto_api_base.rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.appkey and self.secret and self.base)

    async def query(self, waybill_no: str, phone: str = "") -> LogisticsResult:
        if not self.ready:
            return LogisticsResult(
                ok=False, waybill_no=waybill_no, source=self.name,
                error="申通开放平台未配置（STO_APPKEY/STO_SECRET/STO_API_BASE）",
            )
        try:
            payload = await self._call_api(waybill_no, phone)
        except NotImplementedError:
            return LogisticsResult(
                ok=False, waybill_no=waybill_no, source=self.name,
                error="申通开放平台适配器尚未实现（请在 logistics/sto_open.py 填 _call_api）",
            )
        except Exception as exc:
            return LogisticsResult(
                ok=False, waybill_no=waybill_no, source=self.name,
                error=f"申通开放平台请求失败：{type(exc).__name__}",
            )
        return self._normalize(waybill_no, payload)

    async def _call_api(self, waybill_no: str, phone: str = "") -> dict[str, Any]:
        # ==== TODO: 按申通开放平台文档实现 ====
        # 1) 组装业务参数
        # 2) 按文档规定的算法生成 sign（常见为 MD5(appkey+secret+timestamp+data)）
        # 3) POST 到 self.base + 实际路径
        # 4) 返回解析后的 JSON
        raise NotImplementedError

    def _normalize(self, waybill_no: str, payload: dict[str, Any]) -> LogisticsResult:
        """把申通返回结构映射成统一结果。字段名按实际文档调整。"""
        data = payload.get("data") or {}
        traces = data.get("traces") or data.get("list") or []
        if not traces:
            return LogisticsResult(ok=True, found=False, waybill_no=waybill_no,
                                   carrier="申通快递", source=self.name)
        events = [
            LogisticsEvent(
                time=str(t.get("time") or t.get("acceptTime") or ""),
                status=str(t.get("desc") or t.get("acceptDesc") or "").strip(),
                location=str(t.get("site") or t.get("areaName") or ""),
            )
            for t in traces
            if isinstance(t, dict)
        ]
        latest = events[0] if events else None
        state = str(data.get("state") or data.get("status") or "")
        return LogisticsResult(
            ok=True, found=True, waybill_no=waybill_no, carrier="申通快递",
            state=state, signed="签收" in (latest.status if latest else ""),
            latest_time=latest.time if latest else "",
            latest_status=latest.status if latest else "",
            events=list(reversed(events)), source=self.name,
            raw_digest=json.dumps(payload, ensure_ascii=False)[:500],
        )
