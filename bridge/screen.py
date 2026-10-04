"""macOS 屏幕层：找窗口、截图、裁剪、点击、按键。

这一层完全不碰微信进程 —— 只是"看屏幕"和"操作鼠标键盘"，
和一个人在电脑前做的事没有区别。
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import Quartz

from . import ax


@dataclass
class WindowInfo:
    window_id: int
    pid: int
    title: str
    x: int
    y: int
    w: int
    h: int

    def screen_point(self, rx: float, ry: float) -> tuple[float, float]:
        """窗口内归一化坐标 → 全局屏幕坐标（点）。"""
        return (self.x + rx * self.w, self.y + ry * self.h)


def list_windows(pid: int, min_size: int = 200) -> list[WindowInfo]:
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    out: list[WindowInfo] = []
    for w in wins or []:
        if w.get("kCGWindowOwnerPID") != pid:
            continue
        if w.get("kCGWindowLayer") != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
        if width < min_size or height < min_size:
            continue
        out.append(WindowInfo(
            window_id=int(w.get("kCGWindowNumber", 0)),
            pid=pid,
            title=str(w.get("kCGWindowName") or ""),
            x=int(b.get("X", 0)), y=int(b.get("Y", 0)),
            w=width, h=height,
        ))
    out.sort(key=lambda i: -(i.w * i.h))
    return out


def capture_window(window_id: int, path: str | Path) -> str:
    """用系统 screencapture 抓指定窗口。比 CGWindowListCreateImage 稳。"""
    path = str(path)
    r = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-o", "-l", str(window_id), path],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not Path(path).exists():
        raise RuntimeError(f"截屏失败：{r.stderr.strip() or r.returncode}")
    return path


def image_hash(path: str | Path) -> str:
    """整图哈希，用来判断画面有没有变（变了才做 OCR，省时间）。"""
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def image_size(path: str | Path) -> tuple[int, int]:
    src = Quartz.CGImageSourceCreateWithURL(
        Quartz.CFURLCreateWithFileSystemPath(None, str(path), Quartz.kCFURLPOSIXPathStyle, False),
        None,
    )
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    return (Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img))


# ---------------- 鼠标 / 键盘 ----------------

def click(x: float, y: float) -> None:
    pos = Quartz.CGPointMake(float(x), float(y))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved,
                                                      pos, Quartz.kCGMouseButtonLeft))
    time.sleep(0.08)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown,
                                                      pos, Quartz.kCGMouseButtonLeft))
    time.sleep(0.05)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp,
                                                      pos, Quartz.kCGMouseButtonLeft))
    time.sleep(0.15)


def scroll(x: float, y: float, clicks: int = -5) -> None:
    """滚轮。clicks 为负表示向下滚。"""
    pos = Quartz.CGPointMake(float(x), float(y))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved,
                                                      pos, Quartz.kCGMouseButtonLeft))
    for _ in range(abs(clicks)):
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, 1 if clicks > 0 else -1
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def type_text_via_clipboard(text: str, submit: bool = False) -> None:
    """中文没法用虚拟键码敲，统一走剪贴板粘贴。"""
    ax.set_clipboard(text)
    time.sleep(0.12)
    ax.paste()
    time.sleep(0.25)
    if submit:
        ax.key(ax.KEY_RETURN)
        time.sleep(0.3)


def select_all_and_delete() -> None:
    ax.key(ax.KEY_A, ax.CMD)
    time.sleep(0.1)
    ax.key(Quartz.kCGKeyCodeDelete if hasattr(Quartz, "kCGKeyCodeDelete") else 51)
    time.sleep(0.1)
