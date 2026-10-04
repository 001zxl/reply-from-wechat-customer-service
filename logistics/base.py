"""物流服务商协议。任何实现都必须满足：
1. 网络/鉴权失败 → ok=False、error 写清原因，found=False。
2. 查无此单 → ok=True、found=False。
3. 绝不返回猜测出来的轨迹。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas import LogisticsResult

# 快递100 的 state 字段 → 中文状态
KD100_STATE = {
    "0": "在途",
    "1": "已揽收",
    "2": "疑难件",
    "3": "已签收",
    "4": "退签",
    "5": "派件中",
    "6": "退回中",
    "7": "转投",
    "8": "清关中",
    "9": "拒签",
    "10": "派送失败",
    "11": "已转投",
    "12": "清关异常",
    "13": "拒收",
    "14": "拒收",
}


@runtime_checkable
class LogisticsProvider(Protocol):
    name: str

    async def query(self, waybill_no: str, phone: str = "") -> LogisticsResult:
        ...


def is_mock_source(source: str) -> bool:
    return source.strip().lower().startswith("mock")
