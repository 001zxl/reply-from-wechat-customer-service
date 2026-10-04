"""物流查询层：可插拔。查不到就说查不到，绝不编造。"""

from __future__ import annotations

from app.config import settings


def get_provider(name: str | None = None):
    """按配置返回物流服务商。默认 mock（联调）。

    生产环境务必把 LOGISTICS_PROVIDER 切成 kuaidi100 或 sto，
    否则 policy 层会拒绝一切涉及物流状态的自动发送。
    """
    key = (name or settings.logistics.provider or "mock").lower()
    if key == "kuaidi100":
        from .kuaidi100 import Kuaidi100Provider

        return Kuaidi100Provider()
    if key == "sto":
        from .sto_open import StoOpenProvider

        return StoOpenProvider()
    from .mock import MockProvider

    return MockProvider()
