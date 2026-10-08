"""用 macOS 系统自带的 Vision 框架做中文 OCR。

为什么用它：
- 系统内置，不用装 Tesseract / PaddleOCR，也不用联网
- 中文识别质量很好，而且返回每个文本块的**归一化坐标**，
  这正是我们切分「会话列表 / 消息区 / 输入框」所需要的信息
- 坐标原点在**左下角**（Vision 的约定），y 向上增大
"""

from __future__ import annotations

from typing import Iterable, Optional

import Quartz
import Vision
from Foundation import NSURL

# 纯 Python 那部分搬到了 ocr_types，这里 re-export 保持向后兼容：
# 老代码写 `from bridge.vision_ocr import TextBox` 照样能用，
# 但**不依赖平台的模块**应该改从 bridge.ocr_types 导入。
from bridge.ocr_types import (  # noqa: F401
    TextBox,
    group_lines,
    line_text,
    render_debug,
)


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


