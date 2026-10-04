"""快递100 实时查询。

签名方式：sign = MD5(param + key + customer).upper()
文档：https://api.kuaidi100.com/document/5f0ffb5ebc8b3c7d2c6b0dbb

注意：
- 实时查询是按次计费/限流的，正式生产建议改用「订阅推送」(poll) 把轨迹落到本地库，
  避免每次商家问一句就查一次。
- com 编码申通为 shentong。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from app.schemas import LogisticsEvent, LogisticsResult
from app.config import settings
from .base import KD100_STATE

QUERY_URL = "https://poll.kuaidi100.com/poll/query.do"
_CACHE: dict[str, tuple[float, LogisticsResult]] = {}
_CACHE_TTL = 120.0


def _sign(param: str, key: str, customer: str) -> str:
    return hashlib.md5((param + key + customer).encode("utf-8")).hexdigest().upper()


class Kuaidi100Provider:
    name = "kuaidi100"

    def __init__(self) -> None:
        cfg = settings.logistics
        self.customer = cfg.kd100_customer
        self.key = cfg.kd100_key
        self.com = cfg.kd100_com or "shentong"

    @property
    def ready(self) -> bool:
        return bool(self.customer and self.key)

    async def query(self, waybill_no: str, phone: str = "") -> LogisticsResult:
        if not self.ready:
            return LogisticsResult(
                ok=False, waybill_no=waybill_no, source=self.name,
                error="未配置 KD100_CUSTOMER / KD100_KEY，物流查询不可用",
            )

        cache_key = f"{self.com}:{waybill_no}:{phone}"
        hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]

        payload: dict[str, Any] = {"com": self.com, "num": waybill_no, "resultv2": "1"}
        if phone:
            payload["phone"] = phone
        param = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        form = {
            "customer": self.customer,
            "sign": _sign(param, self.key, self.customer),
            "param": param,
        }

        try:
            async with httpx.AsyncClient(timeout=12.0) as cli:
                resp = await cli.post(
                    QUERY_URL, data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            data = resp.json()
        except Exception as exc:  # 网络异常一律视作查询失败
            return LogisticsResult(
                ok=False, waybill_no=waybill_no, source=self.name,
                error=f"快递100 请求失败：{type(exc).__name__}",
            )

        result = self._parse(waybill_no, data)
        if result.ok:
            _CACHE[cache_key] = (time.time(), result)
        return result

    def _parse(self, waybill_no: str, data: dict[str, Any]) -> LogisticsResult:
        if not isinstance(data, dict):
            return LogisticsResult(ok=False, waybill_no=waybill_no, source=self.name,
                                   error="快递100 返回格式异常")

        if data.get("result") is False or str(data.get("status")) not in ("200", ""):
            return LogisticsResult(
                ok=False, waybill_no=waybill_no, source=self.name,
                error=f"{data.get('returnCode', '')} {data.get('message', '查询被拒绝')}".strip(),
            )

        raw_events = data.get("data") or []
        if not raw_events:
            return LogisticsResult(ok=True, found=False, waybill_no=waybill_no,
                                   carrier="申通快递", source=self.name)

        events = [
            LogisticsEvent(
                time=str(item.get("ftime") or item.get("time") or ""),
                status=str(item.get("context") or "").strip(),
                location=str(item.get("areaName") or item.get("areaCode") or ""),
            )
            for item in raw_events
            if isinstance(item, dict)
        ]
        # 快递100 默认倒序，最新在第一条
        latest = events[0] if events else None
        state_code = str(data.get("state", ""))
        return LogisticsResult(
            ok=True,
            found=True,
            waybill_no=waybill_no,
            carrier=str(data.get("com") or "申通快递"),
            state=KD100_STATE.get(state_code, state_code or "未知"),
            signed=state_code in ("3",),
            latest_time=latest.time if latest else "",
            latest_status=latest.status if latest else "",
            events=list(reversed(events)),  # 统一成时间正序
            source=self.name,
        )
