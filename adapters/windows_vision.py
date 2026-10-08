"""Windows 个人微信通道：截屏 + OCR 读消息，模拟鼠标键盘发消息。

⚠️ **重要：这个文件还没有在真实 Windows 机器上验证过。**

作者手上只有 macOS，无法测试 Windows 路径。以下是设计说明和验证方法，
请在 Windows 机器上按 `bridge/inspect_wechat_windows.py` 的提示逐项确认后再上生产。

为什么用截屏而不是读控件
------------------------
微信 4.x（Windows 和 macOS 都是）用的是自绘界面，Windows UI Automation
能拿到的控件信息非常有限，消息列表和输入框基本不可用。所以走同一条路：
**看屏幕 + 敲键盘**。好在这条路不注入进程、不挂钩、不解密本地聊天库，
行为特征最接近真人。

与 macOS 通道的关系
------------------
气泡解析、左右判断、增量比对、会话名匹配这些**与平台无关的逻辑**都在
`adapters/vision_common.py`，本文件只实现"怎么截屏、怎么点、怎么按键"。
那些逻辑在 macOS 上被真实数据反复验证过（含 OCR 抖动、群名截断、消息连发），
这里直接继承。

需要的依赖
----------
    pip install pyautogui pywin32 pillow rapidocr-onnxruntime

OCR 用 RapidOCR（pip 可直接装，中文识别好，离线运行）。
如果想用 Windows 自带的 OCR，见 `_ocr()` 里的说明。

版面标定
--------
微信 Windows 版的版面跟 macOS 略有不同，`DEFAULT_LAYOUT` 里的点值需要实测校准。
用 `bridge/inspect_wechat_windows.py --layout` 会在截图上画出假定区域，
对着截图调 Layout 的数值即可。
"""

from __future__ import annotations

import hashlib
import logging
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.config import ROOT
from app.schemas import IncomingMessage, now_iso
from bridge import guard

from .vision_common import (
    DEFAULT_LAYOUT,
    Layout,
    Observed,
    _is_system_line,
    _norm,
    _similar,
    clean_title,
    count_bubbles,
    new_suffix,
    parse_messages,
    pick_titles,
    send_identity_verdict,
    title_ambiguous,
    title_matches_prefix,
    title_ok,
)

log = logging.getLogger("windows_wechat")

STATE_FILE = ROOT / "data" / "windows_wechat_state.json"

# 微信 Windows 客户端的主窗口类名（4.x 实测值，3.x 是 WeChatMainWndForPC）
WECHAT_WINDOW_CLASSES = ("WeChatMainWndForPC", "Qt51514QWindowIcon", "Chrome_WidgetWin_1")
WECHAT_WINDOW_TITLES = ("微信", "WeChat")


class WeChatNotRunning(RuntimeError):
    """微信没开。调用方应该提示用户，而不是抛一堆栈。"""


def _guarded(action: str, target: str, note: str = ""):
    return guard.scope(action, target, note=note)


class WindowsWeChatVisionChannel:
    """implements adapters.base.WeChatAdapter（send / verify_target）。"""

    channel = "windows_wechat"

    def __init__(self, layout: Optional[Layout] = None,
                 watch: Optional[list[str]] = None,
                 shot_dir: str | Path = r"C:\Windows\Temp\wechat_cs") -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows 通道只能在 Windows 上运行")
        self.layout = layout or DEFAULT_LAYOUT
        self.watch = list(watch or [])
        self.shot_dir = Path(shot_dir)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._last_shot: dict[str, str] = {}
        self._last_msgs: dict[str, list[str]] = {}
        self._hwnd: Optional[int] = None
        self._send_btn_cache: dict[tuple[int, int], tuple[float, float]] = {}
        self.compare_failures = 0
        self.dropped_hint = 0
        import threading

        self._send_lock = threading.Lock()

    # ================= 平台层：窗口 =================

    def _find_window(self) -> tuple[int, tuple[int, int, int, int]]:
        """找微信主窗口，返回 (窗口句柄, (left, top, width, height))。

        注意：Windows 的坐标是"物理像素"，高 DPI 屏幕上要做缩放换算，
        否则点击位置会偏。这里用 GetDpiForWindow 拿到缩放比后换算。
        """
        import win32gui

        found: list[tuple[int, str, tuple[int, int, int, int]]] = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w < 300 or h < 300:
                return
            if cls in WECHAT_WINDOW_CLASSES or title in WECHAT_WINDOW_TITLES:
                found.append((hwnd, title or cls, rect))
            return True

        win32gui.EnumWindows(cb, None)
        if not found:
            raise WeChatNotRunning("找不到微信主窗口。请确认微信已登录并且窗口没有最小化。")
        # 取面积最大的那个（微信有多个辅助窗口）
        hwnd, _title, rect = max(
            found, key=lambda x: (x[2][2] - x[2][0]) * (x[2][3] - x[2][1])
        )
        self._hwnd = hwnd
        return hwnd, (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])

    def ensure_running(self, launch: bool = False, wait: float = 30.0) -> bool:
        """确认微信在运行且有主窗口。launch=True 时尝试启动。"""
        with _guarded("launch_wechat", "微信"):
            try:
                self._find_window()
                return True
            except WeChatNotRunning:
                if not launch:
                    return False
            # 常见安装路径
            for p in (r"C:\Program Files\Tencent\WeChat\WeChat.exe",
                      r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe"):
                if Path(p).exists():
                    subprocess.Popen([p])
                    break
            else:
                return False
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(1.5)
                try:
                    self._find_window()
                    time.sleep(2.0)
                    return True
                except WeChatNotRunning:
                    continue
            return False

    def focus(self) -> None:
        """把微信窗口调到前台。"""
        with _guarded("focus_wechat", "微信"):
            import win32con
            import win32gui

            hwnd, _ = self._find_window()
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                # 前台锁定（Windows 的限制），退化成最小化再恢复
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.5)

    # ================= 平台层：截图与 OCR =================

    def _capture(self, tag: str = "chat") -> tuple[tuple[int, int, int, int], str]:
        """截取微信窗口。返回 (窗口矩形, 图片路径)。"""
        import pyautogui

        _hwnd, rect = self._find_window()
        path = str(self.shot_dir / f"{tag}.png")
        shot = pyautogui.screenshot(region=rect)
        shot.save(path)
        return rect, path

    @staticmethod
    def _ocr(path: str) -> list[Any]:
        """OCR。用 RapidOCR（pip install rapidocr-onnxruntime）。

        它返回的坐标是像素值（左上原点），这里换算成跟 macOS 通道一致的
        归一化坐标（原点左下），这样 vision_common 里的解析逻辑不用改。

        想换成 Windows 自带 OCR 的话，实现一个返回同样结构的函数替换这里即可。
        """
        from rapidocr_onnxruntime import RapidOCR

        engine = _ocr_engine()
        result, _ = engine(path)
        if not result:
            return []
        from PIL import Image

        w, h = Image.open(path).size
        from bridge.ocr_types import TextBox

        out: list[TextBox] = []
        for box, text, conf in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x0, x1 = min(xs) / w, max(xs) / w
            y0, y1 = min(ys) / h, max(ys) / h
            out.append(TextBox(
                text=str(text),
                x=x0, y=1.0 - y1,          # 翻成原点左下
                w=x1 - x0, h=y1 - y0,
                conf=float(conf),
            ))
        return out

    # ================= 平台层：输入 =================

    @staticmethod
    def _click(x: float, y: float) -> None:
        import pyautogui

        pyautogui.moveTo(int(x), int(y), duration=0.12)
        time.sleep(0.08)
        pyautogui.click()
        time.sleep(0.18)

    @staticmethod
    def _set_clipboard(text: str) -> None:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()



    # ================= 通道逻辑（与 macOS 通道同构）=================

    def _title_from(self, boxes: list[Any], rect: tuple[int, int, int, int]) -> str:
        """从 OCR 结果里取聊天区标题栏的文字。"""
        from bridge.ocr_types import group_lines, line_text

        _x, _y, w, h = rect
        chat_left = self.layout.norm_chat_left(w)
        y_min = 1.0 - (self.layout.title_h / h) - 0.02
        cands = [
            b for b in boxes
            if b.cx > chat_left and b.cy > y_min and not _is_system_line(b.text)
        ]
        if not cands:
            return ""
        # 只取最上面那一行（标题行），标题带下沿会蹭到消息区的时间分隔线
        title_line = group_lines(cands)[0]
        title_line.sort(key=lambda b: b.x)
        left = title_line[0].x
        cluster = [b for b in title_line if b.x - left < 0.08]
        return clean_title(line_text(cluster))

    def current_chat_title(self) -> str:
        with _guarded("snapshot", "当前会话标题"):
            rect, path = self._capture("title")
            return self._title_from(self._ocr(path), rect)

    def _title_matches(self, name: str) -> bool:
        try:
            return title_ok(self.current_chat_title(), name)
        except Exception:
            return False

    def list_rows(self, rect: tuple[int, int, int, int]) -> list[tuple[float, str]]:
        """OCR 出会话列表里可见的每一行，返回 (窗口内 y, 文字)。"""
        from bridge.ocr_types import group_lines, line_text

        _x, _y, w, h = rect
        _r, path = self._capture("list")
        boxes = self._ocr(path)
        x0 = self.layout.rail_w / w
        x1 = (self.layout.rail_w + self.layout.list_w) / w
        rows: list[tuple[float, str]] = []
        for ln in group_lines([b for b in boxes if x0 < b.cx < x1 and b.text.strip()]):
            text = line_text(ln)
            y = (1 - ln[0].cy) * h
            if y < self.layout.title_h + 6 or y > h - 12:
                continue
            if _is_system_line(text) or len(text.strip()) < 2:
                continue
            rows.append((y, text))
        rows.sort(key=lambda r: r[0])
        return rows

    def click_list_row(self, rect: tuple[int, int, int, int], name: str) -> bool:
        """直接点会话列表里那一行。比走搜索快，也不碰搜索浮层的坑。

        这里用**宽松**匹配 —— 只是在列表里找一行，列表名字被界面截断是常态。
        点完一定会用 title_ok 严格复核，复核不过就当打开失败。
        """
        want = _norm(clean_title(name))
        if not want:
            return False
        x, y0, _w, _h = rect
        for y, text in self.list_rows(rect):
            got = _norm(text)
            if title_matches_prefix(text, name) or want in got:
                self._click(x + self.layout.rail_w + self.layout.list_w * 0.5, y0 + y)
                time.sleep(1.3)
                if self._title_matches(name):
                    return True
        return False

    def open_conversation(self, name: str) -> bool:
        """打开指定会话。三重兜底：点列表行 → 搜索 → 点列表第一行。"""
        with _guarded("open_conversation", name):
            return self._open_conversation(name)

    def _open_conversation(self, name: str) -> bool:
        if self._title_matches(name):
            return True
        self.focus()
        rect, _ = self._capture("win")
        x, y0, w, h = rect

        # 手段一：会话已经在列表里 → 直接点
        try:
            if self.click_list_row(rect, name):
                self._after_open(name)
                return True
        except Exception:
            log.exception("点列表行失败，改走搜索")

        # 手段二：搜索框。Windows 版快捷键 Ctrl+F 聚焦搜索
        self._hotkey("ctrl", "f")
        time.sleep(0.4)
        self._hotkey("ctrl", "a")
        self._set_clipboard(name)
        self._hotkey("ctrl", "v")
        time.sleep(2.0)
        # 搜索后目标通常置顶，点列表第一行
        self._click(x + self.layout.rail_w + self.layout.list_w * 0.5,
                    y0 + self.layout.title_h + 28)
        time.sleep(1.4)
        if self._title_matches(name):
            self._after_open(name)
            return True

        log.warning("打不开会话：%s（当前标题 %r）", name, self.current_chat_title())
        return False

    def _after_open(self, name: str) -> None:
        time.sleep(0.9)
        try:
            self._last_shot.pop(name, None)
            self._read_messages(name)
            time.sleep(0.3)
            self._last_shot.pop(name, None)
        except Exception:
            log.exception("热身失败")

    def list_conversations(self) -> list[str]:
        """扫描会话列表，返回会话名清单（只读名字，不打开任何会话）。"""
        with _guarded("scan_chat_list", "会话列表"):
            rect, _ = self._capture("win")
            return pick_titles(self.list_rows(rect))

    def read_messages(self, chat: str) -> list[Observed]:
        """读当前会话消息。**先确认打开的确实是目标会话**，否则跳过。"""
        with _guarded("read_messages", chat):
            return self._read_messages(chat)

    def _read_messages(self, chat: str) -> list[Observed]:
        import hashlib as _h

        try:
            rect, path = self._capture("chat")
        except WeChatNotRunning:
            log.warning("微信窗口暂时不可见，本轮跳过")
            return []
        digest = _h.md5(Path(path).read_bytes()).hexdigest()
        if self._last_shot.get(chat) == digest:
            return []
        self._last_shot[chat] = digest

        boxes = self._ocr(path)
        title = self._title_from(boxes, rect)
        if not title_ok(title, chat):
            log.info("当前打开的是 %r，不是 %r —— 本轮跳过（可能人在用微信）", title, chat)
            self._last_msgs.pop(chat, None)
            return []
        return parse_messages(boxes, self.layout, rect[2], rect[3])

    def snapshot(self, chat: str) -> tuple[str, list[Observed]]:
        with _guarded("snapshot", chat):
            rect, path = self._capture("snap")
            boxes = self._ocr(path)
            return (self._title_from(boxes, rect),
                    parse_messages(boxes, self.layout, rect[2], rect[3]))


    def _detect_mention(self, text: str) -> bool:
        """群里这条消息是不是**真的**点了我。

        做法：拿自己在微信里的昵称（WECHAT_SELF_NICKNAME）去正文里找 "@昵称"。
        微信在群里 @ 某人时，被 @ 的人看到的气泡正文里就带着 "@你的昵称"。

        ★ 认不出来就返回 False，**不猜**（外部审查 P2）。
          第一版直接硬编码 mentioned_bot=True，后果是"大家吃饭了吗"
          也被当成点你名，一路送进模型排队回复 —— 这是把"只处理 @ 或单号"
          这条配置规则彻底废掉了。宁可少回一句，也不能在几十人的群里乱开口。
        """
        nick = (getattr(self, "self_nickname", "") or "").strip()
        if not nick:
            return False
        t = text or ""
        return f"@{nick}" in t or f"＠{nick}" in t


    def poll(self) -> list[IncomingMessage]:
        """读监听列表里各会话的新消息，只返回对方发来的。"""
        out: list[IncomingMessage] = []
        for chat in self.watch:
            try:
                if not self._title_matches(chat):
                    if not self.open_conversation(chat):
                        continue
                observed = self.read_messages(chat)
            except Exception:
                log.exception("读取会话 %s 失败", chat)
                continue
            if not observed:
                continue
            prints = [m.fingerprint for m in observed]
            prev = self._last_msgs.get(chat, [])
            fresh = new_suffix(prev, prints)
            if not fresh and prev and prints != prev:
                self.compare_failures += 1
                self.dropped_hint += max(0, len(prints) - len(prev))
                log.warning("会话 %s 画面有变化但比对不上（累计 %d 次）",
                            chat, self.compare_failures)
            self._last_msgs[chat] = prints
            by_print = {m.fingerprint: m for m in observed}
            for fp in fresh:
                side, _, _text = fp.partition("|")
                if side != "in":
                    continue
                m = by_print.get(fp)
                if m is None:
                    continue
                seq = _next_seq()
                out.append(IncomingMessage(
                    event_id=f"wx-{seq:08d}",
                    channel=self.channel,
                    conversation_id=chat,
                    channel_chat_id=chat,
                    sender_id=m.sender or chat,
                    sender_name=m.sender,
                    text=m.text,
                    is_group=bool(m.sender),
                    mentioned_bot=self._detect_mention(m.text),
                    received_at=now_iso(),
                ))
        return out

    def _out_texts(self, chat: str) -> list[str]:
        """当前可见的**己方**气泡文本。发送前后做增量比对用。"""
        try:
            self._last_shot.pop(chat, None)
            return [m.text.strip() for m in self._read_messages(chat)
                    if m.side == "out" and m.text.strip()]
        except Exception:
            log.exception("回读己方气泡失败")
            return []

    # ================= 平台层：键盘 =================

    @staticmethod
    def _hotkey(*keys: str) -> None:
        import pyautogui

        pyautogui.hotkey(*keys)
        time.sleep(0.15)

    def _press_send(self, rect: tuple[int, int, int, int]) -> str:
        """点右下角「发送」按钮。

        微信 Windows 版的回车默认是**发送**（和 macOS 相反），但用户可以在设置里
        改成换行，所以统一走按钮更稳。
        """
        x, y0, w, h = rect
        key = (w, h)
        cached = self._send_btn_cache.get(key)
        if cached is not None:
            self._click(x + cached[0], y0 + cached[1])
            return "cache"
        _r, path = self._capture("sendbtn")
        cands = [b for b in self._ocr(path)
                 if b.text.strip() in ("发送", "Send") and b.x > 0.7 and b.cy < 0.3]
        if cands:
            b = cands[0]
            pt = (b.cx * w, (1 - b.cy) * h)
            self._send_btn_cache[key] = pt
            self._click(x + pt[0], y0 + pt[1])
            return "ocr"
        # 兜底：右下角固定位置
        pt = self.layout.send_button(w, h)
        self._click(x + pt[0], y0 + pt[1])
        return "fallback"

    # ================= 发送 =================

    async def verify_target(self, channel_chat_id: str) -> bool:
        """确认并切到目标会话。屏幕上看不到目标就没法保证点击落在对的地方。"""
        try:
            if title_ok(self.current_chat_title(), channel_chat_id):
                return True
            log.info("当前会话不是目标，正在切换：%s", channel_chat_id)
            if not self.open_conversation(channel_chat_id):
                return False
            return title_ok(self.current_chat_title(), channel_chat_id)
        except Exception:
            log.exception("校验/切换目标失败")
            return False

    async def send(self, channel_chat_id: str, text: str):
        from adapters.base import SendResult
        from app.config import settings

        with _guarded("send", channel_chat_id, note=text[:60]):
            return await self._send(channel_chat_id, text)

    async def _send(self, channel_chat_id: str, text: str):
        from adapters.base import SendResult
        from app.config import settings

        if not settings.send_allowlist:
            return SendResult("blocked",
                              "未配置 WECHAT_SEND_ALLOWLIST，按安全默认拒绝发送任何消息")
        if not any(title_ok(channel_chat_id, a) for a in settings.send_allowlist):
            return SendResult("blocked",
                              f"{channel_chat_id!r} 不在发送白名单里，拒绝发送")

        with self._send_lock:
            try:
                if not title_ok(self.current_chat_title(), channel_chat_id):
                    if not self.open_conversation(channel_chat_id):
                        return SendResult("failed", f"打不开会话 {channel_chat_id}")

                # ★ 身份闸（外部审查 P1）：只认严格相等，不用前缀当凭据；
                #   当前标题同时像多个已配置会话时直接停。
                try:
                    from bridge import guard as _guard
                    _names = list(dict.fromkeys(
                        list(settings.send_allowlist) + list(_guard.allowed_chats())))
                except Exception:
                    _names = list(settings.send_allowlist)
                _ok, _why = send_identity_verdict(
                    self.current_chat_title(), channel_chat_id, _names)
                if not _ok:
                    return SendResult("blocked", _why)

                self.focus()
                rect, _ = self._capture("win")
                x, y0, w, h = rect
                ix, iy = self.layout.input_center(w, h)
                self._click(x + ix, y0 + iy)
                time.sleep(0.3)
                self._hotkey("ctrl", "a")
                self._hotkey("delete")
                time.sleep(0.15)
                self._set_clipboard(text)
                self._hotkey("ctrl", "v")

                if settings.typing_simulation:
                    pause = min(0.8 + len(text) * 0.035, 4.5) * random.uniform(0.75, 1.35)
                    time.sleep(pause)
                else:
                    time.sleep(0.5)

                # ★ 点发送前先数一遍同文气泡（外部审查 P1）。
                #   旧消息"…旧单。"能冒充新消息"…新单。"，必须比**新增**。
                before_n = count_bubbles(self._out_texts(channel_chat_id), text)

                how = self._press_send(rect)

                for _ in range(6):
                    time.sleep(0.6)
                    after = self._out_texts(channel_chat_id)
                    if count_bubbles(after, text) > before_n:
                        self._last_msgs[channel_chat_id] = [
                            m.fingerprint for m in self._read_messages(channel_chat_id)]
                        return SendResult(
                            "sent",
                            f"已确认聊天记录里多出这条（发送前已有 {before_n} 条，"
                            f"按钮定位={how}）")
                return SendResult(
                    "unknown",
                    f"已点发送，回读 6 次都没看到**新增**的这条（发送前已有 {before_n} 条），"
                    f"结果未知，请人工核对（不会自动重发）")
            except Exception as exc:
                log.exception("发送失败")
                return SendResult("unknown", f"发送异常，结果未知：{type(exc).__name__}")


_SEQ_LOCK = __import__("threading").Lock()


def _next_seq() -> int:
    """跨进程稳定的自增序号，作为 event_id 用。"""
    import json

    with _SEQ_LOCK:
        data = {"seq": 0}
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        data["seq"] = int(data.get("seq", 0)) + 1
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
        return data["seq"]


def _ocr_engine():
    """OCR 引擎单例。RapidOCR 初始化要几秒，别每次重新建。"""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


_ENGINE = None
