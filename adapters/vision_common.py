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
        # 归一非文字消息的 OCR 噪音（语音气泡碎片 → [语音 N秒]），
        # 否则模型会把 '3"（' 当成商家打的字
        text = normalize_media_text("\n".join(line_text(l) for l in block).strip())
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

# 时长后面跟的引号，OCR 认不准是哪种，全都认
_TIME_QUOTES = "\"\u201c\u201d'\u2019"

# 语音气泡的 OCR 特征。微信的语音消息只画一个气泡加时长，没有文字，
# 但 OCR 会从气泡和按钮上读出一堆噪音。实测（2026-10）出现过：
#     '3"（'      '• 3"'      '• 3" • 转文字'
# 不归一的话，模型会把这些碎片当成商家打的字。
VOICE_ARTIFACT_RE = re.compile(
    r"^[\u2022\u00b7\u3002\s]*\d{1,3}\s*[" + _TIME_QUOTES + r"]?\s*[\uff08(]?\s*$"
)
VOICE_MARKERS = ("转文字", "转成文字")


def normalize_media_text(text: str) -> str:
    """把非文字消息的 OCR 噪音归一成明确的占位标记。

    商家发语音/图片是常态，但这些消息没有可读文字，OCR 只会读到气泡边框
    和按钮上的碎片。不归一的话，模型会把碎片当成商家打的字。

    注意：**图片里的文字是识别不出来的**（那需要图像理解）。
    OCR 会把图上的印刷字读出来当普通文本，这一层区分不了，
    靠提示词里的一条规则来兜（见 app/prompts.py 的非文字消息规则）。
    """
    t = (text or "").strip()
    if not t or len(t) > 16:
        return t                      # 太长的不可能是气泡噪音

    # 语音：短、含"数字+引号"、可能带"转文字"按钮
    if any(m in t for m in VOICE_MARKERS) or VOICE_ARTIFACT_RE.match(t):
        m = re.search(r"(\d{1,3})\s*[" + _TIME_QUOTES + r"]", t)
        secs = f" {m.group(1)}秒" if m else ""
        return f"[语音{secs}]"

    if t in ("[图片]", "[视频]", "[文件]", "[动画表情]", "[位置]", "[链接]"):
        return t                      # 已经是占位符

    return t


# 会话列表标题后跟的时间戳："10:07" / "昨天" / "昨天 10:07" / "星期三" / "12/25"
LIST_TIME_RE = re.compile(
    r"\s*(?:"
    r"(?:昨天|前天|星期[一二三四五六日])\s*\d{1,2}:\d{2}"
    r"|\d{1,2}:\d{2}"
    r"|昨天|前天|星期[一二三四五六日]"
    r"|\d{1,2}/\d{1,2}"
    r"|\d{1,2}月\d{1,2}日"
    r")\s*$"
)


def strip_list_time(text: str) -> str:
    """去掉会话名尾部的时间戳和省略号，得到干净的名字。"""
    return re.sub(r"[.．·…]+$", "", LIST_TIME_RE.sub("", text).strip()).strip()


# 同一条会话的标题行和预览行之间的纵向间距上限（像素）。
# 实测：标题到预览约 20px，两条会话之间约 45-65px，取 32 分得开。
LIST_ROW_GAP = 32.0


def pick_titles(rows: list[tuple[float, str]]) -> list[str]:
    """从会话列表的文字行里挑出"标题行"。

    ★ 这里踩过坑：**不能靠"有没有时间戳"判断标题行。**
    实测微信列表长这样：

        文件传输助手              ← 没有预览、没有时间，但这就是标题
        腾讯新闻          20:42   ← 标题
        油价调整通知               ← 预览
        测试1                     ← 没有时间，是标题
        微信团队                   ← 没有时间，是标题

    按时间戳过滤会漏掉一大半会话（实测 5 个只认出 1 个）。
    正确做法是按纵向间距聚类：挨得近的行属于同一条会话，取最上面那行当标题。
    """
    out: list[str] = []
    prev: float | None = None
    for y, text in sorted(rows, key=lambda r: r[0]):
        if prev is None or (y - prev) > LIST_ROW_GAP:
            name = strip_list_time(text)
            if len(name) >= 2:
                out.append(name)
        prev = y
    return out


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
