"""按配置选择微信通道。"""

from __future__ import annotations

from app.config import settings


def get_adapter(channel: str | None = None):
    name = (channel or settings.channel or "mock").lower()
    if name == "wecom_kf":
        from .wecom_kf import WeComKfChannel

        return WeComKfChannel()
    if name in ("wcferry_win", "wcferry"):
        from .wcferry_win import WcFerryChannel

        return WcFerryChannel()
    if name in ("macos_wechat", "macos", "mac_vision"):
        from .macos_vision import MacWeChatVisionChannel

        return MacWeChatVisionChannel(watch=settings.macos_watch)
    if name in ("windows_wechat", "windows", "win_vision"):
        from .windows_vision import WindowsWeChatVisionChannel

        return WindowsWeChatVisionChannel(
            watch=tuple(x.strip() for x in
                        __import__("os").environ.get("WECHAT_WIN_WATCH", "").split(",") if x.strip())
        )
    from .mock_channel import MockChannel

    return MockChannel()
