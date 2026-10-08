"""OCR 的**纯 Python 数据结构和排版逻辑**（不依赖任何平台）。

★ 为什么要单独拆出来（外部审查 P1）：
  第一版把 `TextBox` / `group_lines` / `line_text` 和 Quartz/Vision 放在同一个
  文件 `bridge/vision_ocr.py` 里。导入链就变成了
      windows_vision → vision_common → bridge.vision_ocr → Quartz
  而 Windows 的依赖安装条件**不会装** Quartz —— 于是 Windows 上一 import
  适配器就 `ModuleNotFoundError: No module named 'Quartz'`，
  整条 Windows 通道根本起不来。

  这三个东西一行平台代码都没有，纯粹是"文本框 + 按行聚类"。
  放在这里之后：
      macos_vision   → bridge.vision_ocr （用 Quartz 做真 OCR）
      windows_vision → bridge.vision_ocr （用 RapidOCR 做真 OCR）
  两边共用同一份 vision_common 和同一份 ocr_types，互不牵连。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextBox:
    text: str
    x: float          # 归一化左边界 0~1
    y: float          # 归一化下边界 0~1（原点左下）
    w: float
    h: float
    conf: float = 1.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def top(self) -> float:
        return self.y + self.h

    @property
    def bottom(self) -> float:
        return self.y


def group_lines(boxes: list[TextBox], y_tol: float = 0.012) -> list[list[TextBox]]:
    """把 OCR 出的文本块按纵向位置聚成"行"，行内按 x 排序。

    Vision 会把同一行的不同片段拆成多个 box，这里还原成行。
    """
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: (-b.cy, b.x))
    lines: list[list[TextBox]] = []
    for box in ordered:
        if lines and abs(lines[-1][0].cy - box.cy) <= y_tol:
            lines[-1].append(box)
        else:
            lines.append([box])
    for line in lines:
        line.sort(key=lambda b: b.x)
    return lines


def line_text(line: list[TextBox], sep: str = " ") -> str:
    return sep.join(b.text for b in line).strip()


def render_debug(boxes: list[TextBox], width: int = 100, height: int = 30) -> str:
    """把 OCR 结果画成字符网格，方便肉眼确认版面切得对不对。"""
    grid = [[" "] * width for _ in range(height)]
    for b in boxes:
        row = int((1 - b.cy) * (height - 1))
        col = int(b.x * (width - 1))
        row = max(0, min(height - 1, row))
        for i, ch in enumerate(b.text[:6]):
            c = col + i
            if 0 <= c < width:
                grid[row][c] = ch
    return "\n".join("".join(r) for r in grid)
