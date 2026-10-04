"""本地假数据。只用于联调与离线测试。

安全设计：source 固定为 "mock"，policy 层看到 mock 会拒绝任何自动发送，
所以假物流永远不可能被误发给商家。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from app.schemas import LogisticsEvent, LogisticsResult

KNOWN = {
    "773123456789012": "transit",
    "773987654321098": "signed",
    "773000000000001": "returning",
}


class MockProvider:
    name = "mock"

    async def query(self, waybill_no: str, phone: str = "") -> LogisticsResult:
        num = waybill_no.strip()
        if len(num) < 10:
            return LogisticsResult(
                ok=True, found=False, waybill_no=num, carrier="申通快递", source="mock",
                error="",
            )

        kind = KNOWN.get(num)
        if kind is None:
            # 没登记的单号一律返回"查不到"。
            # 之前这里对任意单号都编一份假轨迹，导致"查不到"这条路径
            # 根本没被测试覆盖到 —— 而那恰恰是最容易出幻觉的地方。
            return LogisticsResult(
                ok=True, found=False, waybill_no=num,
                carrier="申通快递", source="mock",
            )

        base = datetime.now() - timedelta(hours=30)
        seq = [
            ("已揽收", "浙江省杭州市余杭区"),
            ("运输中", "杭州转运中心"),
            ("运输中", "江苏省南京市转运中心"),
            ("派件中", "南京市建邺区网点"),
        ]
        if kind == "signed":
            seq.append(("已签收", "南京市建邺区网点，签收人：本人"))
        elif kind == "returning":
            seq.append(("退回中", "南京市建邺区网点，退回件"))

        events = [
            LogisticsEvent(
                time=(base + timedelta(hours=i * 6)).strftime("%Y-%m-%d %H:%M:%S"),
                status=status,
                location=loc,
            )
            for i, (status, loc) in enumerate(seq)
        ]
        latest = events[-1]
        state = {"transit": "派件中", "signed": "已签收", "returning": "退回中"}[kind]
        return LogisticsResult(
            ok=True, found=True, waybill_no=num, carrier="申通快递",
            state=state, signed=kind == "signed",
            latest_time=latest.time, latest_status=f"{latest.status} {latest.location}",
            events=events, source="mock",
        )
