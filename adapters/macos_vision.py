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

log = logging.getLogger("macos_wechat")

WECHAT_BUNDLE = "com.tencent.xinWeChat"
WECHAT_APP = "/Applications/WeChat.app"
STATE_FILE = ROOT / "data" / "macos_wechat_state.json"


# ---------------------------------------------------------------- 版面

@dataclass
class Layout:
    """窗口内坐标。竖向用"距底部多少点"表示，横向用点数表示，
    因为微信的图标栏/会话列表是固定宽度，不会随窗口缩放。"""

    rail_w: float = 80.0          # 图标栏宽度（点）
    list_w: float = 226.0         # 会话列表宽度（点）
    title_h: float = 48.0         # 聊天标题栏高度（点）
    input_h: float = 164.0        # 底部输入区高度（点）
    search_xy: tuple[float, float] = (165.0, 27.0)   # 搜索框中心（窗口内点坐标）

    def chat_left(self, w: float) -> float:
        return min(self.rail_w + self.list_w, w * 0.5)

    def norm_chat_left(self, w: float) -> float:
        return self.chat_left(w) / w

    def input_center(self, w: float, h: float) -> tuple[float, float]:
        left = self.chat_left(w)
        return (left + (w - left) * 0.47, h - self.input_h * 0.62)

    def send_button(self, w: float, h: float) -> tuple[float, float]:
        """右下角「发送」按钮的兜底位置（优先用 OCR 实际找）。"""
        return (w - 47.0, h - 34.0)

    def message_band(self, h: float) -> tuple[float, float]:
        """消息区在 Vision 归一化坐标（原点左下）下的 y 范围。

        窗口从上到下是：标题栏 / 消息区 / 输入区。
        Vision 的 y 向上增大，所以：
          消息区上沿 = 1 - 标题栏高度占比
          消息区下沿 = 输入区高度占比
        """
        bottom = self.input_h / h
        top = 1.0 - self.title_h / h
        return (bottom, top)

    def incoming_left(self, w: float) -> float:
        """对方气泡的左边缘（归一化）：头像 + 间距。"""
        return (self.chat_left(w) + 58.0) / w

    def outgoing_right(self, w: float) -> float:
        """自己气泡的右边缘（归一化）：固定贴着右侧留出头像位。"""
        return (w - 81.0) / w


DEFAULT_LAYOUT = Layout()


# ---------------------------------------------------------------- 观察到的消息

@dataclass
class Observed:
    side: str            # in | out
    text: str
    sender: str = ""
    top: float = 0.0     # Vision 归一化 y（越大越靠上）

    @property
    def fingerprint(self) -> str:
        return f"{self.side}|{self.text.strip()}"


def _classify(box: TextBox, in_left: float, out_right: float) -> str:
    """判断这条消息是自己发的还是对方发的。

    不能只看左边缘或右边缘：一条很长的对方消息也可能横跨大半个窗口。
    可靠的特征是**对齐边** —— 对方气泡永远左对齐，自己的永远右对齐。
    所以比较"左边缘离对方基准线的距离"和"右边缘离自己基准线的距离"。
    """
    d_in = abs(box.x - in_left)
    d_out = abs(box.right - out_right)
    return "out" if d_out < d_in else "in"


SYSTEM_LINE_RE = re.compile(
    r"^(\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}|昨天|今天|星期[一二三四五六日])"
    r"|撤回了一条消息|邀请.*加入|开启了朋友验证|以上是打招呼"
)


def _is_system_line(text: str) -> bool:
    return bool(SYSTEM_LINE_RE.search(text.strip()))


def parse_messages(boxes: list[TextBox], layout: Layout,
                   win_w: float, win_h: float) -> list[Observed]:
    """把 OCR 文本块还原成一条条消息。

    两个要点：
    1. 先按纵向间距把行聚成"气泡"，**整块**判断方向和内容。
       逐行判断会出错 —— 同一气泡里每行的 x 都不一样（文字只是气泡内换行）。
    2. 时间分隔线（"9月25日 19:00"）居中对齐，两个基准都不贴，要单独剔除。
    """
    chat_left = layout.norm_chat_left(win_w)
    in_left = layout.incoming_left(win_w)
    out_right = layout.outgoing_right(win_w)
    y_low, y_high = layout.message_band(win_h)

    inside = [
        b for b in boxes
        if b.cx > chat_left and y_low < b.cy < y_high and b.text.strip()
    ]
    if not inside:
        return []

    # 1) 按纵向间距聚成气泡
    blocks: list[list[list[TextBox]]] = []
    for line in group_lines(inside):
        if blocks:
            gap = blocks[-1][-1][0].cy - line[0].cy
            if gap < 0.055:
                blocks[-1].append(line)
                continue
        blocks.append([line])

    # 2) 整块判断
    messages: list[Observed] = []
    for block in blocks:
        flat = [b for line in block for b in line]
        text = "\n".join(line_text(l) for l in block).strip()
        if not text or _is_system_line(text):
            continue
        left = min(b.x for b in flat)
        right = max(b.right for b in flat)
        if abs(right - out_right) < abs(left - in_left):
            side = "out"
        else:
            side = "in"
        messages.append(Observed(
            side=side, text=text, top=max(b.top for b in flat),
        ))

    messages.sort(key=lambda m: -m.top)

    # 群里发言人名字有时会被切成独立的一块（"Pea"、"黄林"）。
    # 短、单行、无句末标点、紧贴着下一条 → 判为下一条的发言人。
    merged: list[Observed] = []
    for m in messages:
        if (merged and m.side == "in" and merged[-1].side == "in"
                and len(m.text) <= 8 and "\n" not in m.text
                and not m.text.endswith(("。", "！", "？", "!", "?", ".", "~"))
                and (merged[-1].top - m.top) < 0.075):
            merged[-1].sender = m.text.strip()
            merged[-1].text = f"{m.text.strip()}：{merged[-1].text}"
            merged[-1].top = m.top
            continue
        merged.append(m)
    return merged


def _regroup(boxes: list[TextBox]) -> list[list[TextBox]]:
    """把收集到的块重新按行聚合，保证行内按 x 排序。"""
    return group_lines(boxes)


# ---------------------------------------------------------------- 增量比对

SIMILARITY_THRESHOLD = 0.80


def _similar(a: str, b: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """两条消息算不算"同一条"。

    不能要求完全相等 —— OCR 每次识别都有细微抖动
    （少一个标点、把"未"认成"末"），精确比对会让整段对齐崩掉，
    结果是**静默漏消息**，这是最危险的失效方式。

    但也不能放太松，否则人工滚动窗口时会把"完全不同的两条"判成同一条，
    于是重复回复。所以分两档：
      - 长消息按相似度
      - 短消息（≥6 字符）额外容忍**一个错字**，因为一个字对短句的影响太大
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > max(4, 0.3 * max(la, lb)):
        return False
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= threshold:
        return True
    if min(la, lb) >= 6 and abs(la - lb) <= 1 and ratio >= 0.72:
        return True
    return False


def new_suffix(prev: list[str], cur: list[str]) -> list[str]:
    """找出 cur 里相对 prev 新增的尾部。

    为什么不用"文本哈希去重"：两条一模一样的"催一下"会被误判成同一条。
    这里用的是**位置对齐** —— 找最大的 k，使 prev 的后 k 条与 cur 的前 k 条
    逐条相似，那么 cur 剩下的就是真正新增的。同样的文本出现在不同位置也能区分。

    返回 [] 表示"对不上"（人工滚动了窗口，或画面还没渲染完）。
    这时宁可漏也不重复 —— 但调用方必须记账，不能假装无事发生。
    """
    if not prev or not cur:
        return []
    max_k = min(len(prev), len(cur))
    for k in range(max_k, 0, -1):
        if all(
            _similar(prev[len(prev) - k + i], cur[i])
            for i in range(k)
        ):
            return cur[k:]
    return []


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
    def read_messages(self, chat: str) -> list[Observed]:
        with _guarded("read_messages", chat):
            return self._read_messages(chat)

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
        return parse_messages(boxes, self.layout, win.w, win.h)

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

def _norm(text: str) -> str:
    """归一化：去掉所有空白和常见分隔符，用于名称比对。

    OCR 经常把"大潍坊AI交流群"读成"大潍坊 AI 交流群"，
    直接字符串相等会漏掉，所以必须先归一化。
    """
    return re.sub(r"[\s\u00a0·・\-—_]+", "", text or "").lower()


TITLE_NOISE_RE = re.compile(r"[（(]\s*\d+\s*[)）].*$")


def clean_title(raw: str) -> str:
    """清洗从标题栏读到的文字。

    群聊标题会带成员数（"大潍坊AI交流群（304）"）和右侧的图标噪音，
    直接比对永远不相等，也会让"发送前校验"拦下本来正确的发送。
    """
    t = TITLE_NOISE_RE.sub("", raw or "")
    t = re.sub(r"\s+", "", t)
    return t.strip(" •。·、|,，")


MIN_PREFIX_MATCH = 4


def title_ok(actual: str, want: str) -> bool:
    """读到的标题和配置的名字算不算同一个会话。

    会话列表里的名字会被截断显示（"京东生活线报群6禁.."），用户配的时候
    往往只能看到截断版，所以**允许配置名是真名的前缀** —— 但要求至少 4 个字，
    避免"客户A"误配到"客户AB"上。
    """
    a, w = _norm(clean_title(actual)), _norm(clean_title(want))
    if not a or not w:
        return False
    if a == w:
        return True
    return len(w) >= MIN_PREFIX_MATCH and a.startswith(w)


def _sender_of(text: str, chat: str) -> str:
    """群聊里气泡上方会有一行发言人名字，这里粗略取第一行。
    单聊没有名字，返回空串。"""
    first, _, rest = text.partition("\n")
    if rest and len(first) <= 16 and not first.endswith(("。", "！", "？", ":", "：")):
        return first.strip()
    return ""
