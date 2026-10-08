"""外部系统集成层。

网点要用到的"别的软件"通常长这三种样子：

  1. HTTP 接口  —— 申通开放平台、快递100、网点自己的 ERP REST 接口
  2. 本机命令   —— 很多网点是 Excel + 批处理脚本，或者有个命令行小工具
  3. 桌面软件   —— 只能看界面操作的老系统（比如只能在 Windows 客户端里点）
                  这种用 bridge/screen.py + OCR 那套做法，跟微信通道同源

风险分级是这层的核心设计：
  read  —— 只读查询，模型可以随便调，结果直接喂给它
  write —— 会产生真实副作用的操作（拦截、改址、催派）
           **模型调不动它**，只能登记成待办转人工
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Risk = Literal["read", "write"]


@dataclass
class ActionResult:
    ok: bool
    text: str = ""                      # 回灌给模型的事实文本
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    source: str = ""

    def to_model_text(self) -> str:
        if not self.ok:
            return f"调用失败：{self.error}。禁止据此编造结果。"
        return self.text or "（对方系统没有返回可用信息）"


@dataclass
class ActionSpec:
    """一个可调用的动作。"""

    system: str
    name: str
    label: str
    risk: Risk
    when: str = ""                      # 什么时候该用它（写给模型看的）
    params: list[str] = field(default_factory=list)
    timeout: float = 15.0

    @property
    def callable_by_model(self) -> bool:
        return self.risk == "read"


@runtime_checkable
class Integration(Protocol):
    system: str
    label: str
    description: str
    # ★ 是不是**演示/模拟**数据源（config 里写 "mock": true）。
    #   模拟来源的结果一律不许外发 —— 见 app/pipeline.py 的 mock_flag。
    is_mock: bool

    def actions(self) -> list[ActionSpec]: ...

    async def run(self, action: str, args: dict[str, Any]) -> ActionResult: ...
