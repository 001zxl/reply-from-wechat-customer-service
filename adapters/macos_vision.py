"""macOS 微信通道：截屏 + 系统 OCR 读消息，模拟鼠标键盘发消息。

为什么是这条路
--------------
微信 4.x 在 macOS 上**完全不暴露界面控件**（整个界面是自绘的，AX 只能看到
3 个窗口按钮）。所以读不到控件树，只能"看屏幕"。

这条路的好处是它**完全不碰微信进程** —— 不注入、不挂钩、不解密本地库，
只是截屏 + 识别文字 + 移动鼠标 + 按键，和人坐在电脑前做的事没有区别。
坏处是慢（一次 OCR 约 0.6 秒），而且依赖窗口布局。

实测环境：微信 4.1.13 / macOS 26 / 891×514 窗口
需要的两个系统权限：辅助功能、屏幕录制。

版面（窗口内归一化坐标，原点左上）
----------------------------------
    0        0.09      0.343                        1.0
    ├─图标栏──┼──会话列表──┼──────────聊天区─────────┤
    │         │   [搜索]   │ 标题栏                │ 0.092
    │         │            ├───────────────────────┤
    │         │            │      消息区           │
    │         │            │                       │ 0.68
    │         │            ├───────────────────────┤
    │         │            │  [输入框]      [发送] │ 1.0
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import random
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.config import ROOT
from app.schemas import IncomingMessage, now_iso
from bridge import ax, guard, screen
from bridge.vision_ocr import TextBox, group_lines, line_text, ocr_image

# 与平台无关的逻辑都在这里，Windows 通道共用同一份
from .vision_common import (
    DEFAULT_LAYOUT,
    Layout,
    Observed,
    _is_system_line,
    _norm,
    _similar,
    clean_title,
    find_media_regions,
    new_suffix,
    parse_messages,
    pick_titles,
    title_ok,
)

log = logging.getLogger("macos_wechat")

WECHAT_BUNDLE = "com.tencent.xinWeChat"
WECHAT_APP = "/Applications/WeChat.app"
STATE_FILE = ROOT / "data" / "macos_wechat_state.json"


# ---------------------------------------------------------------- 版面


# ---------------------------------------------------------------- 状态

def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"seq": 0, "chats": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 通道

def _guarded(action: str, target: str, note: str = ""):
    """包一层授权闸。已经在外层已授权操作的范围内就直通（避免重复审批）。"""
    return guard.scope(action, target, note=note)


class WeChatNotRunning(RuntimeError):
    """微信没开。调用方应该提示用户，而不是抛一堆栈。"""


class MacWeChatVisionChannel:
    """implements adapters.base.WeChatAdapter（send / verify_target）。"""

    channel = "macos_wechat"

    def __init__(self, layout: Optional[Layout] = None,
                 watch: Optional[list[str]] = None,
                 shot_dir: str | Path = "/tmp/wechat_cs") -> None:
        self.layout = layout or DEFAULT_LAYOUT
        self.watch = list(watch or [])          # 要监听的会话名；空表示只发不收
        self.shot_dir = Path(shot_dir)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.state = _load_state()
        self._last_shot: dict[str, str] = {}    # chat → 上次截图哈希
        self._last_msgs: dict[str, list[str]] = {}
        self._pid: Optional[int] = None
        self._send_lock = __import__("threading").Lock()
        self._send_btn_cache: dict[tuple[int, int], tuple[float, float]] = {}
        # 增量比对失败次数。这个数字直接对应"可能漏掉的消息条数"，要盯。
        self.compare_failures = 0
        self.dropped_hint = 0

    # ---------------- 基础 ----------------
    def _require_pid(self) -> int:
        pid = ax.find_pid(WECHAT_BUNDLE)
        if not pid:
            raise WeChatNotRunning("微信没有运行")
        self._pid = pid
        return pid

    def ensure_running(self, launch: bool = False, wait: float = 30.0) -> bool:
        with _guarded("launch_wechat", "微信"):
            return self._ensure_running(launch, wait)

    def _ensure_running(self, launch: bool = False, wait: float = 30.0) -> bool:
        """确认微信处于可用状态（进程在、已登录、主窗口在）。

        三种情况都要处理，实测都遇到过：
          1. 进程没在      → 拉起来
          2. 停在"进入微信"确认页 → 点一下（不需要扫码，会话还在）
          3. 主窗口还没渲染出来 → 等
        """
        if not ax.find_pid(WECHAT_BUNDLE):
            if not launch:
                return False
            subprocess.run(["open", "-a", WECHAT_APP], check=False)

        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(1.5)
            pid = ax.find_pid(WECHAT_BUNDLE)
            if not pid:
                continue
            if [w for w in screen.list_windows(pid, 300) if w.title == "微信"]:
                return True
            # 主窗口不在，看看是不是停在"进入微信"确认页
            if self._click_enter_wechat(pid):
                time.sleep(3.0)
        return bool(ax.find_pid(WECHAT_BUNDLE)) and bool(
            [w for w in screen.list_windows(ax.find_pid(WECHAT_BUNDLE), 300)
             if w.title == "微信"]
        )

    def _click_enter_wechat(self, pid: int) -> bool:
        """微信重启后会停在「进入微信」页，点一下就恢复，不用扫码。"""
        for w in screen.list_windows(pid, 100):
            if w.title != "微信" or w.w > 600:
                continue
            try:
                path = screen.capture_window(w.window_id, self.shot_dir / "login.png")
                hit = [b for b in ocr_image(path) if "进入微信" in b.text]
            except Exception:
                continue
            if hit:
                b = hit[0]
                log.info("微信停在登录确认页，点「进入微信」")
                screen.click(w.x + b.cx * w.w, w.y + (1 - b.cy) * w.h)
                return True
        return False

    def focus(self) -> None:
        with _guarded("focus_wechat", "微信"):
            self._focus()

    def _focus(self) -> None:
        """把微信调到前台。

        注意：NSRunningApplication.activateWithOptions_ 在 macOS 14+ 上
        从后台进程调用经常无效，`open -a` 才是可靠的。
        """
        subprocess.run(["open", "-a", WECHAT_APP], check=False)
        time.sleep(1.1)

    def main_window(self, retries: int = 4):
        """找微信主窗口。加短暂重试 —— 切桌面、最小化、全屏动画期间会瞬间找不到。"""
        pid = self._require_pid()
        for attempt in range(retries):
            wins = [w for w in screen.list_windows(pid, 300) if w.title == "微信"]
            if wins:
                return wins[0]
            time.sleep(0.5)
        raise RuntimeError("找不到微信主窗口（标题为“微信”的窗口）")

    def screenshot(self, tag: str = "chat"):
        win = self.main_window()
        path = screen.capture_window(win.window_id, self.shot_dir / f"{tag}.png")
        return win, path

    # ---------------- 定位会话 ----------------
    def _title_from(self, boxes: list[TextBox], win) -> str:
        chat_left = self.layout.norm_chat_left(win.w)
        y_title_min = 1.0 - (self.layout.title_h / win.h) - 0.02
        cands = [
            b for b in boxes
            if b.cx > chat_left and b.cy > y_title_min and not _is_system_line(b.text)
        ]
        if not cands:
            return ""
        # 只取最上面那一行（标题行）。标题带下沿会蹭到消息区里的时间分隔线，
        # 全捞进来会拼成"文件传输助手12:45"这种鬼东西。
        title_line = group_lines(cands)[0]
        title_line.sort(key=lambda b: b.x)
        left = title_line[0].x
        # 标题有时会被切成两块，取左边缘挨着的那一簇
        cluster = [b for b in title_line if b.x - left < 0.08]
        return clean_title(line_text(cluster))

    def current_chat_title(self) -> str:
        """读聊天区标题栏。用来做发送前校验。"""
        with _guarded("snapshot", "当前会话标题"):
            win, path = self.screenshot("title")
            return self._title_from(ocr_image(path), win)

    def list_rows(self, win) -> list[tuple[float, str]]:
        """OCR 出会话列表里可见的每一行，返回 (窗口内 y 坐标, 文字)。"""
        path = screen.capture_window(win.window_id, self.shot_dir / "list.png")
        boxes = ocr_image(path)
        x0 = self.layout.rail_w / win.w
        x1 = (self.layout.rail_w + self.layout.list_w) / win.w
        lines = group_lines([b for b in boxes if x0 < b.cx < x1 and b.text.strip()])
        rows: list[tuple[float, str]] = []
        for ln in lines:
            text = line_text(ln)
            y = (1 - ln[0].cy) * win.h
            if y < self.layout.title_h + 6 or y > win.h - 12:
                continue
            if _is_system_line(text) or len(text.strip()) < 2:
                continue
            rows.append((y, text))
        rows.sort(key=lambda r: r[0])
        return rows

    def click_list_row(self, win, name: str) -> bool:
        """直接点会话列表里那一行。比走搜索快得多，也不会碰到搜索浮层的坑。"""
        want = _norm(clean_title(name))
        if not want:
            return False
        for y, text in self.list_rows(win):
            got = _norm(text)
            if got.startswith(want) or want in got:
                x = win.x + self.layout.rail_w + self.layout.list_w * 0.5
                screen.click(x, win.y + y)
                time.sleep(1.4)
                if self._title_matches(name):
                    return True
        return False

    def open_conversation(self, name: str) -> bool:
        with _guarded("open_conversation", name):
            return self._open_conversation(name)

    def _open_conversation(self, name: str) -> bool:
        """打开指定会话。四重兜底，因为这个动作失败后面全都白搭。

        微信的搜索结果分三段：群聊/联系人 → 聊天记录 → 搜索网络结果。
        必须点最上面那段（真正的会话条目），点"聊天记录"只会打开记录搜索。

        注意：打开成功后**不要按 ESC** —— 会把刚打开的聊天面板关掉，
        变成"列表选中但右侧空白"的状态。搜索框里的残留文字不影响收发。
        """
        if title_ok(self.current_chat_title(), name):
            return True

        self.focus()
        win = self.main_window()

        # 手段一：目标会话已经显示在列表里 → 直接点那一行
        try:
            if self.click_list_row(win, name):
                self._after_open(name)
                return True
        except Exception:
            log.exception("点列表行失败，改走搜索")

        win = self.main_window()
        sx, sy = win.screen_point(
            self.layout.search_xy[0] / win.w, self.layout.search_xy[1] / win.h
        )
        screen.click(sx, sy)
        time.sleep(0.5)
        ax.key(ax.KEY_A, ax.CMD)
        time.sleep(0.15)
        screen.type_text_via_clipboard(name)
        time.sleep(2.2)

        # 手段二：抓整屏找搜索结果条目（搜索下拉是独立浮层，抓窗口抓不到）
        full = self.shot_dir / "search.png"
        try:
            subprocess.run(["/usr/sbin/screencapture", "-x", str(full)], check=True)
            cands = self._result_rows(str(full), win, name)
        except Exception:
            log.exception("搜索结果解析失败")
            cands = []

        for pt in cands[:3]:
            screen.click(*pt)
            time.sleep(1.5)
            if self._title_matches(name):
                self._after_open(name)
                return True

        # 手段三：直接回车，打开搜索框里高亮的那一项
        ax.key(ax.KEY_RETURN)
        time.sleep(1.6)
        if self._title_matches(name):
            self._after_open(name)
            return True

        # 手段四：点会话列表第一行（搜索之后目标会被置顶）
        first_row = win.screen_point(
            (self.layout.rail_w + self.layout.list_w * 0.5) / win.w,
            (self.layout.title_h + 28.0) / win.h,
        )
        screen.click(*first_row)
        time.sleep(1.5)
        if self._title_matches(name):
            self._after_open(name)
            return True

        log.warning("三种方式都没打开会话：%s（当前标题 %r）", name, self.current_chat_title())
        ax.key(ax.KEY_ESC)
        time.sleep(0.4)
        return False

    def _title_matches(self, name: str) -> bool:
        try:
            return title_ok(self.current_chat_title(), name)
        except Exception:
            return False

    def _after_open(self, name: str) -> None:
        """打开后等渲染完，并丢掉第一帧，避免半渲染画面污染基线。"""
        time.sleep(0.9)
        self._warm_up(name)

    def _warm_up(self, chat: str) -> None:
        """切完会话先读一帧丢掉。

        刚切过去的画面常常还没渲染完，这一帧的 OCR 结果会带上残缺文本，
        一旦被当成基线，后面每一轮都对不上，就会一直漏消息。
        """
        try:
            self._last_shot.pop(chat, None)
            self.read_messages(chat)
            time.sleep(0.4)
            self._last_shot.pop(chat, None)
        except Exception:
            log.exception("热身失败")

    def _result_rows(self, full_png: str, win, name: str) -> list[tuple[float, float]]:
        """返回候选条目的点击坐标（屏幕点），从屏幕上方往下排。"""
        import Quartz
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        disp_w, disp_h = bounds.size.width, bounds.size.height

        want = _norm(name)
        list_x0 = win.x + self.layout.rail_w
        list_x1 = win.x + self.layout.rail_w + self.layout.list_w
        y_from = win.y + self.layout.title_h          # 标题栏以下
        y_to = win.y + win.h

        rows: list[tuple[float, float, float]] = []
        for b in ocr_image(full_png):
            text = _norm(b.text)
            if not text:
                continue
            if not (text == want or text.startswith(want) or want in text):
                continue
            px, py = b.cx * disp_w, (1 - b.cy) * disp_h
            if not (list_x0 - 8 <= px <= list_x1 + 8 and y_from <= py <= y_to):
                continue
            rows.append((py, px, len(text)))
        # 越靠屏幕上方越是"真正的会话条目"；名字越短越可能是本体而不是预览
        rows.sort(key=lambda r: (r[0], r[2]))
        return [(px, py) for py, px, _ in rows]

    # ---------------- 读 ----------------
    # 会话列表里每行标题后面跟的时间戳
    # 会话列表标题后面跟的时间戳，形态有："10:07" / "昨天" / "昨天 10:07" /
    # "星期三" / "12/25" / "12月25日"。要整体剥掉，只留会话名。
    _LIST_TIME_RE = re.compile(
        r"\s*(?:"
        r"(?:昨天|前天|星期[一二三四五六日])\s*\d{1,2}:\d{2}"
        r"|\d{1,2}:\d{2}"
        r"|昨天|前天|星期[一二三四五六日]"
        r"|\d{1,2}/\d{1,2}"
        r"|\d{1,2}月\d{1,2}日"
        r")\s*$"
    )

    def list_conversations(self) -> list[str]:
        """扫描左侧会话列表，返回会话名清单。

        只读名字，不打开任何会话、不读任何聊天内容 —— 这是给"配置白名单"用的。
        标题行的挑法（含踩坑说明）见 vision_common.pick_titles。
        """
        with _guarded("scan_chat_list", "会话列表"):
            win = self.main_window()
            return pick_titles(self.list_rows(win))

    def read_messages(self, chat: str) -> list[Observed]:
        with _guarded("read_messages", chat):
            return self._read_messages(chat)

    def _read_media(self, png: str, boxes, win) -> list[Observed]:
        """把聊天区里的图片/视频区块也变成消息（文字之外的那部分）。"""
        try:
            scale = screen.image_size(png)[0] / win.w if win.w else 2.0
        except Exception:
            scale = 2.0
        out: list[Observed] = []
        for i, r in enumerate(find_media_regions(png, self.layout, win.w, win.h, boxes, scale)):
            # 立刻把图裁出来存盘（不调模型，很快）。
            # 视觉分析放到流水线里做 —— 那里是异步的，不会卡住轮询。
            path = ""
            try:
                from app.media import crop_region

                path = str(crop_region(png, r.pixel_box, tag=f"{r.kind}_{i}")[0])
            except Exception:
                log.exception("裁图失败")
            out.append(Observed(
                side=r.side,
                text="[图片]" if r.kind == "image" else "[视频]",
                top=r.top,
                media=r.kind,
                media_box=r.pixel_box,
                media_path=path,
            ))
        return out

    def _read_messages(self, chat: str) -> list[Observed]:
        """读当前会话的消息。

        关键：**先确认当前打开的确实是要读的那个会话**。
        人在用微信的时候随时会切走（实测 4 次连续读取里切了 3 个会话），
        如果不检查，就会把别的会话的消息当成这个会话的新消息发出去 —— 这是灾难。
        """
        try:
            win = self.main_window()
        except RuntimeError:
            log.warning("微信主窗口暂时不可见，本轮跳过")
            return []

        path = screen.capture_window(win.window_id, self.shot_dir / "chat.png")
        digest = hashlib.md5(Path(path).read_bytes()).hexdigest()
        if self._last_shot.get(chat) == digest:
            return []
        self._last_shot[chat] = digest

        boxes = ocr_image(path)                       # 一次 OCR 同时喂给标题和消息
        title = self._title_from(boxes, win)
        if not title_ok(title, chat):
            log.info("当前打开的是 %r，不是 %r —— 本轮跳过（可能人在用微信）",
                     title, chat)
            self._last_msgs.pop(chat, None)           # 会话被切走，基线作废
            return []
        # 顺序很重要：**先找媒体区域，再解析文字**。
        # 因为商家发来的图片里往往有印刷文字（型号、单号、表格），OCR 会把
        # 它读出来当成一条独立消息（实测收到过 '2204～. ©②'）。
        # 先拿到图片的位置，就能把这些碎片剔掉 —— 图片本身有视觉描述，
        # 这些碎片只会干扰模型。
        media = self._read_media(path, boxes, win)
        kept_boxes = boxes
        if media:
            scale = screen.image_size(path)[0] / win.w if win.w else 2.0
            def _in_media(b) -> bool:
                px, py = b.cx * win.w * scale, (1 - b.cy) * win.h * scale
                for r in media:
                    x0, y0, x1, y1 = r.media_box
                    if x0 - 6 <= px <= x1 + 6 and y0 - 6 <= py <= y1 + 6:
                        return True
                return False

            kept_boxes = [b for b in boxes if not _in_media(b)]

        msgs = parse_messages(kept_boxes, self.layout, win.w, win.h)
        if media:
            msgs = sorted(msgs + media, key=lambda m: -m.top)
        return msgs

    def poll(self) -> list[IncomingMessage]:
        """读取监听列表里各会话的新消息。只返回对方发来的。"""
        out: list[IncomingMessage] = []
        for chat in self.watch:
            try:
                if self.current_chat_title() != chat:
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
                # 画面变了却对不上：可能人工滚动了，也可能有新消息但没认出来
                self.compare_failures += 1
                self.dropped_hint += max(0, len(prints) - len(prev))
                log.warning(
                    "会话 %s 画面有变化但增量比对对不上（累计 %d 次，疑似漏 %d 条）",
                    chat, self.compare_failures, self.dropped_hint,
                )
            self._last_msgs[chat] = prints
            if not fresh:
                continue

            by_print = {m.fingerprint: m for m in observed}
            for fp in fresh:
                side, _, text = fp.partition("|")
                if side != "in":
                    continue                    # 自己发的不用回
                m = by_print.get(fp)
                self.state["seq"] = int(self.state.get("seq", 0)) + 1
                seq = self.state["seq"]
                out.append(IncomingMessage(
                    event_id=f"mw-{seq:08d}",
                    channel=self.channel,
                    conversation_id=chat,
                    channel_chat_id=chat,
                    sender_id=m.sender or chat,
                    sender_name=m.sender,
                    text=text,
                    is_group=bool(m.sender) if m else False,
                    mentioned_bot=True,
                    media=m.media,
                    media_box=m.media_box,
                    media_path=m.media_path,
                    received_at=now_iso(),
                ))
            _save_state(self.state)
        return out

    def snapshot(self, chat: str) -> tuple[str, list[Observed]]:
        with _guarded("snapshot", chat):
            return self._snapshot(chat)

    def _snapshot(self, chat: str) -> tuple[str, list[Observed]]:
        """一次截图同时返回标题和消息，保证两者来自同一帧。"""
        win = self.main_window()
        path = screen.capture_window(win.window_id, self.shot_dir / "snap.png")
        boxes = ocr_image(path)
        return self._title_from(boxes, win), parse_messages(boxes, self.layout, win.w, win.h)

    # ---------------- 发 ----------------
    def _press_send(self, win) -> str:
        """点右下角的「发送」按钮。

        为什么不按回车：微信 Mac 默认**回车是换行不是发送**，
        实测按回车内容只会留在输入框里，看起来"发出去了"其实是假象。
        这里用 OCR 现找按钮，比写死坐标稳。
        """
        key = (win.w, win.h)
        cached = self._send_btn_cache.get(key)
        if cached is not None:
            screen.click(*win.screen_point(cached[0] / win.w, cached[1] / win.h))
            return "cache"

        path = screen.capture_window(win.window_id, self.shot_dir / "sendbtn.png")
        cands = [
            b for b in ocr_image(path)
            if b.text.strip() == "发送" and b.x > 0.7 and b.cy < 0.3
        ]
        if cands:
            b = cands[0]
            pt = (b.cx * win.w, (1 - b.cy) * win.h)
            self._send_btn_cache[key] = pt      # 同一窗口尺寸下位置不会变，缓存起来
            screen.click(*win.screen_point(pt[0] / win.w, pt[1] / win.h))
            return "ocr"
        bx, by = self.layout.send_button(win.w, win.h)
        screen.click(*win.screen_point(bx / win.w, by / win.h))
        return "fallback"

    async def verify_target(self, channel_chat_id: str) -> bool:
        """确认并**切到**目标会话。

        对界面自动化来说，"校验"和"切换"是一件事：屏幕上看不到目标会话，
        就没法保证后面的点击和粘贴落在对的地方。所以这里先看当前标题，
        不对就主动用搜索切过去，切完再确认一次。
        """
        try:
            if title_ok(self.current_chat_title(), channel_chat_id):
                return True
            log.info("当前会话不是目标，正在切换：%s", channel_chat_id)
            if not self.open_conversation(channel_chat_id):
                return False
            ok = title_ok(self.current_chat_title(), channel_chat_id)
            if not ok:
                log.warning("切换后标题仍不匹配：%r", self.current_chat_title())
            return ok
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

        # 硬闸：没在白名单里，一条都不许发。
        # 这个检查放在最前面，任何调用路径都绕不过去。
        if not settings.send_allowlist:
            return SendResult(
                "blocked",
                "未配置 WECHAT_SEND_ALLOWLIST，按安全默认拒绝发送任何消息",
            )
        if not any(title_ok(channel_chat_id, allowed)
                   for allowed in settings.send_allowlist):
            return SendResult(
                "blocked",
                f"{channel_chat_id!r} 不在发送白名单 {list(settings.send_allowlist)} 里，拒绝发送",
            )

        with self._send_lock:                     # 全局串行，别让两个会话抢窗口
            try:
                if not title_ok(self.current_chat_title(), channel_chat_id):
                    if not self.open_conversation(channel_chat_id):
                        return SendResult("failed", f"打不开会话 {channel_chat_id}")
                    if not title_ok(self.current_chat_title(), channel_chat_id):
                        return SendResult(
                            "failed",
                            f"打开后标题仍不匹配（读到 {self.current_chat_title()!r}），拒绝发送",
                        )

                self.focus()
                win = self.main_window()
                ix, iy = self.layout.input_center(win.w, win.h)
                screen.click(*win.screen_point(ix / win.w, iy / win.h))
                time.sleep(0.35)

                # 先把输入框里可能残留的内容清掉，别把上一条一起发出去
                ax.key(ax.KEY_A, ax.CMD)
                time.sleep(0.12)
                ax.key(ax.KEY_DELETE)
                time.sleep(0.12)

                screen.type_text_via_clipboard(text)

                # 风控 6：打字节奏。粘贴是瞬间完成的，真人不会打完立刻发送。
                # 停顿时长跟内容长度相关，且带随机，避免固定间隔。
                if settings.typing_simulation:
                    pause = min(0.8 + len(text) * 0.035, 4.5)
                    pause *= random.uniform(0.75, 1.35)
                    time.sleep(pause)
                else:
                    time.sleep(0.6)

                how = self._press_send(win)

                # 回读确认。消息渲染 + 列表滚动需要时间，一次读不到不代表没发出去，
                # 所以要重试几次 —— 否则每条都报 unknown，审核台会被假警报淹没。
                # 回读比对必须用模糊匹配：OCR 会把"末"认成"未"，
                # 精确子串判断会把发出去的消息误报成 unknown。
                probe = text.strip()[:14]
                observed: list[Observed] = []
                for _ in range(6):
                    time.sleep(0.6)
                    self._last_shot.pop(channel_chat_id, None)
                    observed = self.read_messages(channel_chat_id)
                    if any(m.side == "out" and _similar(probe, m.text[:14])
                           for m in observed):
                        self._last_msgs[channel_chat_id] = [m.fingerprint for m in observed]
                        return SendResult("sent", f"已确认出现在聊天记录里（按钮定位={how}）")
                return SendResult(
                    "unknown",
                    f"已点发送，回读 {len(observed)} 条仍未确认，请人工核对（不会自动重发）",
                )
            except Exception as exc:
                log.exception("发送失败")
                return SendResult("unknown", f"发送异常，结果未知：{type(exc).__name__}")

def _sender_of(text: str, chat: str) -> str:
    """群聊里气泡上方会有一行发言人名字，这里粗略取第一行。
    单聊没有名字，返回空串。"""
    first, _, rest = text.partition("\n")
    if rest and len(first) <= 16 and not first.endswith(("。", "！", "？", ":", "：")):
        return first.strip()
    return ""
