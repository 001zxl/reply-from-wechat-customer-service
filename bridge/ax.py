"""macOS Accessibility (AX) 底层工具。

macOS 上想让程序读别的 App 的界面、模拟按键，只有这一条路：
Accessibility API。它需要用户在「系统设置 → 隐私与安全性 → 辅助功能」
里给运行本程序的 App 打勾（终端 / iTerm / DeepSeek Harness）。

这里只做四件事：找进程、找窗口、遍历控件、模拟按键。
"""

from __future__ import annotations

import time
from typing import Any, Iterator, Optional

import ApplicationServices as AS
import Quartz
from AppKit import NSRunningApplication, NSWorkspace

# 常用键盘码
KEY_RETURN = 36
KEY_TAB = 48
KEY_ESC = 53
KEY_V = 9
KEY_A = 0
KEY_F = 3
KEY_DELETE = 51
KEY_DOWN = 125
KEY_UP = 126

CMD = Quartz.kCGEventFlagMaskCommand


class AccessibilityDenied(RuntimeError):
    pass


def ensure_trusted(prompt: bool = True) -> None:
    """没授权就抛错，并把系统授权弹窗叫出来。"""
    if AS.AXIsProcessTrusted():
        return
    if prompt:
        AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    raise AccessibilityDenied(
        "没有辅助功能（Accessibility）权限。\n"
        "请打开 系统设置 → 隐私与安全性 → 辅助功能，\n"
        "把你运行本程序的 App（终端 / iTerm / DeepSeek Harness）打上勾，然后重新运行。"
    )


# ---------------- 进程 ----------------

def find_pid(bundle_id: str) -> Optional[int]:
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == bundle_id:
            return int(app.processIdentifier())
    return None


def activate(pid: int) -> None:
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        app.activateWithOptions_(1 << 1)   # NSApplicationActivateIgnoringOtherApps
        time.sleep(0.4)


# ---------------- 控件 ----------------

def attr(el: Any, name: str) -> Any:
    try:
        err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:
        return None


def role(el: Any) -> str:
    return str(attr(el, AS.kAXRoleAttribute) or "")


def title(el: Any) -> str:
    return str(attr(el, AS.kAXTitleAttribute) or "")


def value(el: Any) -> str:
    v = attr(el, AS.kAXValueAttribute)
    return "" if v is None else str(v)


def description(el: Any) -> str:
    return str(attr(el, AS.kAXDescriptionAttribute) or "")


def children(el: Any) -> list[Any]:
    kids = attr(el, AS.kAXChildrenAttribute)
    return list(kids) if kids else []


def app_element(pid: int) -> Any:
    return AS.AXUIElementCreateApplication(pid)


def windows(pid: int) -> list[Any]:
    wins = attr(app_element(pid), AS.kAXWindowsAttribute)
    return list(wins) if wins else []


def main_window(pid: int) -> Optional[Any]:
    wins = windows(pid)
    for w in wins:
        if role(w) == "AXWindow":
            return w
    return wins[0] if wins else None


def walk(el: Any, max_depth: int = 12, _depth: int = 0) -> Iterator[tuple[int, Any]]:
    """深度优先遍历控件树。"""
    yield _depth, el
    if _depth >= max_depth:
        return
    for kid in children(el):
        yield from walk(kid, max_depth, _depth + 1)


def find_all(el: Any, *, role_is: Optional[str] = None, max_depth: int = 12) -> list[Any]:
    out = []
    for _, node in walk(el, max_depth):
        if role_is is None or role(node) == role_is:
            out.append(node)
    return out


def describe(el: Any) -> str:
    parts = [role(el)]
    for name, getter in (("title", title), ("value", value), ("desc", description)):
        text = getter(el)
        if text:
            parts.append(f"{name}={text[:80]!r}")
    return " ".join(p for p in parts if p)


def dump(el: Any, max_depth: int = 10) -> str:
    lines = []
    for depth, node in walk(el, max_depth):
        lines.append("  " * depth + describe(node))
    return "\n".join(lines)


# ---------------- 按下 / 按键 ----------------

def press(el: Any) -> bool:
    try:
        return AS.AXUIElementPerformAction(el, AS.kAXPressAction) == 0
    except Exception:
        return False


def set_value(el: Any, text: str) -> bool:
    try:
        return AS.AXUIElementSetAttributeValue(
            el, AS.kAXValueAttribute, text
        ) == 0
    except Exception:
        return False


def focus(el: Any) -> bool:
    try:
        if AS.AXUIElementSetAttributeValue(
            el, AS.kAXFocusedAttribute, True
        ) != 0:
            return False
        AS.AXUIElementPerformAction(el, AS.kAXPressAction)
        return True
    except Exception:
        return False


def set_clipboard(text: str) -> None:
    from AppKit import NSPasteboard, NSPasteboardTypeString

    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def key(code: int, flags: int = 0, times: int = 1) -> None:
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for _ in range(times):
        down = Quartz.CGEventCreateKeyboardEvent(src, code, True)
        up = Quartz.CGEventCreateKeyboardEvent(src, code, False)
        if flags:
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventSetFlags(up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        time.sleep(0.05)


def paste() -> None:
    key(KEY_V, CMD)
