"""截屏类微信通道的公共逻辑（与操作系统无关）。

macOS 和 Windows 两个通道共用这一层：气泡解析、左右判断、增量比对、
会话名匹配。换平台只需要重写"怎么截屏、怎么点、怎么按键"，这些算法不用动。

这样分开的好处：这些逻辑在 macOS 上被真实数据反复验证过（包括 OCR 抖动、
群名截断、消息连发等坑），Windows 通道直接继承，不用重新踩一遍。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Optional

from bridge.vision_ocr import TextBox, group_lines, line_text


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

def _norm(text: str) -> str:
    """归一化：去掉所有空白和常见分隔符，用于名称比对。

    OCR 经常把"某某行业交流群"读成"某某行业 交流群"，
    直接字符串相等会漏掉，所以必须先归一化。
    """
    return re.sub(r"[\s\u00a0·・\-—_]+", "", text or "").lower()


TITLE_NOISE_RE = re.compile(r"[（(]\s*\d+\s*[)）].*$")


def clean_title(raw: str) -> str:
    """清洗从标题栏读到的文字。

    群聊标题会带成员数（"某某行业交流群（304）"）和右侧的图标噪音，
    直接比对永远不相等，也会让"发送前校验"拦下本来正确的发送。
    """
    t = TITLE_NOISE_RE.sub("", raw or "")
    t = re.sub(r"\s+", "", t)
    return t.strip(" •。·、|,，")


MIN_PREFIX_MATCH = 4


def title_ok(actual: str, want: str) -> bool:
    """读到的标题和配置的名字算不算同一个会话。

    会话列表里的名字会被截断显示（"某电商福利群6禁广告.."），用户配的时候
    往往只能看到截断版，所以**允许配置名是真名的前缀** —— 但要求至少 4 个字，
    避免"客户A"误配到"客户AB"上。
    """
    a, w = _norm(clean_title(actual)), _norm(clean_title(want))
    if not a or not w:
        return False
    if a == w:
        return True
    return len(w) >= MIN_PREFIX_MATCH and a.startswith(w)
