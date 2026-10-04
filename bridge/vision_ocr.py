"""用 macOS 系统自带的 Vision 框架做中文 OCR。

为什么用它：
- 系统内置，不用装 Tesseract / PaddleOCR，也不用联网
- 中文识别质量很好，而且返回每个文本块的**归一化坐标**，
  这正是我们切分「会话列表 / 消息区 / 输入框」所需要的信息
- 坐标原点在**左下角**（Vision 的约定），y 向上增大
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import Quartz
import Vision
from Foundation import NSURL


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


def load_cgimage(path: str):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise RuntimeError(f"读不了图片：{path}")
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        raise RuntimeError(f"解析不了图片：{path}")
    return img


def ocr_cgimage(cgimage, languages: Iterable[str] = ("zh-Hans", "en-US"),
                accurate: bool = True) -> list[TextBox]:
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelAccurate if accurate
        else Vision.VNRequestTextRecognitionLevelFast
    )
    req.setRecognitionLanguages_(list(languages))
    req.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"OCR 失败：{err}")

    out: list[TextBox] = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        best = cands[0]
        bb = obs.boundingBox()
        out.append(TextBox(
            text=str(best.string()),
            x=float(bb.origin.x), y=float(bb.origin.y),
            w=float(bb.size.width), h=float(bb.size.height),
            conf=float(best.confidence()),
        ))
    return out


def ocr_image(path: str, **kw) -> list[TextBox]:
    return ocr_cgimage(load_cgimage(path), **kw)


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
