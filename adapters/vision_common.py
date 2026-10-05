"""截屏类微信通道的公共逻辑（与操作系统无关）。

macOS 和 Windows 两个通道共用这一层：气泡解析、左右判断、增量比对、
会话名匹配。换平台只需要重写"怎么截屏、怎么点、怎么按键"，这些算法不用动。

这样分开的好处：这些逻辑在 macOS 上被真实数据反复验证过（包括 OCR 抖动、
群名截断、消息连发等坑），Windows 通道直接继承，不用重新踩一遍。
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("vision")

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
    # 非文字消息：kind 是 "image"/"video"，box 是截图内的像素框
    media: str = ""
    media_box: tuple = ()
    media_path: str = ""     # 已裁好的图片文件，流水线直接拿去分析
    voice_text: str = ""     # 语音转写出来的文字（微信「转文字」的结果）

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
    r"^[\u2022\u00b7\u3002\s]*\d{1,3}\s*[" + _TIME_QUOTES + r"]?"
    r"\s*[\uff08(]?[\u2022\u00b7\u3002\s]*$"
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


# ======================================================================
# 媒体消息检测（图片/视频）
# ======================================================================
#
# 背景：OCR 只能读文字。商家发来的面单截图、实物照片、视频封面，
# OCR 要么读到一堆图上的碎片文字（'Canon 35' '220V'），要么什么都读不到。
# 所以要把图片**本身**截出来送给多模态模型去"看"。
#
# 怎么找：微信聊天区的背景是纯色，文字气泡和图片都是"非背景区块"。
# 实测（2026-10，微信 4.1.13 macOS，880x640 窗口）：
#     文字气泡  高度 27~37 点，填充率 0.69~0.78
#     图片      高度 214 点，填充率 0.32
# **高度是最强的区分信号** —— 气泡再长也很少超过 60 点。

# 超过这个高度（窗口内逻辑点数）就认为不是纯文字气泡
MEDIA_MIN_HEIGHT = 70.0
_MEDIA_COL_DENSE = 0.25     # 一列要有四成内容是内容，才算在图片宽度内
_MEDIA_ROW_DENSE = 0.55     # 一行的图片宽度里要有 55% 是内容
# 纵向间隔小于这个值（逻辑点）的媒体区块要合并成一条
MEDIA_MERGE_GAP = 60.0
# 窗口左右两边这几个点里可能有边框/阴影，分析时要排除
WINDOW_EDGE_INSET = 14.0
# 判定"有内容"的像素差阈值。
# ★ 必须给得小：微信聊天背景是 (250,250,250)，而截图类图片内容是纯白
#   (255,255,255)，**只差 5**。给 40 会把图片本身当成背景。
_MEDIA_PIXEL_DIFF = 10
# 照片类检测用的粗阈值（照片有大片浅色区域）
_MEDIA_COARSE_DIFF = 40
# 行/列上算作"有内容"的占比下限
_MEDIA_ROW_THRESHOLD = 0.05
_MEDIA_COL_THRESHOLD = 0.25


@dataclass
class MediaRegion:
    """聊天区里的一块非文字媒体（图片或视频封面）。"""

    kind: str                                   # "image" | "video"
    side: str                                   # "in" | "out"
    # 归一化坐标 (x0, y0, x1, y1)，原点左下，跟 Observed 一套
    bbox: tuple[float, float, float, float]
    top: float
    # 截图内的像素坐标（直接拿去裁图，原点左上）
    pixel_box: tuple[int, int, int, int]


def text_row_ranges(text_boxes: list[TextBox], layout: Layout,
                    win_w: float, win_h: float) -> list[tuple[float, float]]:
    """文字行占据的 y 范围（逻辑点，原点左上），按上到下排序、相邻的已合并。"""
    chat_left = layout.norm_chat_left(win_w)
    y_low, y_high = layout.message_band(win_h)
    inside = [
        b for b in text_boxes
        if b.cx > chat_left and y_low < b.cy < y_high
        and b.text.strip() and not _is_system_line(b.text)
    ]
    if not inside:
        return []
    ranges: list[tuple[float, float]] = []
    for ln in group_lines(inside):
        top = min((1 - (b.cy + b.h / 2)) for b in ln) * win_h
        bot = max((1 - (b.cy - b.h / 2)) for b in ln) * win_h
        ranges.append((top, bot))
    ranges.sort()
    merged: list[list[float]] = []
    for a, b in ranges:
        if merged and a - merged[-1][1] < 14:      # 同一气泡内的多行
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _content_mask(arr, bg, thr=_MEDIA_PIXEL_DIFF):
    import numpy as np
    return np.abs(arr - bg).sum(axis=2) > thr


def _row_runs(mask, scale, min_height_pt=MEDIA_MIN_HEIGHT,
              row_thr=_MEDIA_ROW_THRESHOLD):
    """按行切出连续的"有内容"段落，只留够高的。"""
    rowpct = mask.sum(axis=1) / max(1, mask.shape[1])
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for y, p in enumerate(rowpct):
        if p > row_thr and start is None:
            start = y
        elif p <= row_thr and start is not None:
            runs.append((start, y))
            start = None
    if start is not None:
        runs.append((start, len(rowpct)))
    return [(a, b) for a, b in runs if (b - a) / scale >= min_height_pt]


def _tighten_box(mask, y0, y1, px0, px_end, scale, min_height_pt):
    """在 y 段内收紧左右，再拿这几列反过来收紧上下。返回像素框或 None。"""
    import numpy as np
    seg = mask[y0:y1]
    if seg.size == 0:
        return None
    colpct = seg.sum(axis=0) / max(1, seg.shape[0])
    nz = np.nonzero(colpct > _MEDIA_COL_DENSE)[0]
    if len(nz) == 0:
        return None
    # 返回的是 crop 内坐标（x 和 y 都是），调用方再统一加偏移。
    # ★ 这里踩过坑：x 曾经在这里加过 px0，调用方又加一遍，坐标就double了。
    bx0, bx1 = int(nz[0]), int(nz[-1]) + 1
    if (bx1 - bx0) / scale < 60:
        return None
    sub = seg[:, int(nz[0]):int(nz[-1]) + 1]
    subrow = sub.sum(axis=1) / max(1, sub.shape[1])
    rnz = np.nonzero(subrow > _MEDIA_ROW_DENSE)[0]
    if len(rnz) == 0:
        return None
    by0, by1 = y0 + int(rnz[0]), y0 + int(rnz[-1]) + 1
    if (by1 - by0) / scale < min_height_pt:
        return None
    return (bx0, by0, bx1, by1)


def find_media_regions(img_path, layout: Layout, win_w: float, win_h: float,
                       text_boxes: Optional[list[TextBox]] = None,
                       scale: float = 1.0) -> list[MediaRegion]:
    """在聊天区里找出图片/视频区块。

    ★ 两种检测取并集，因为不同的图各有各的难点：

    **检测一：按行扫非背景像素（细阈值）**
      适合"截图类"图片。实测那张物流轨迹截图内容是纯白 (255,255,255)，
      而微信聊天背景是 (250,250,250) —— **只差 5**。
      阈值给 40 会把图片本身当成背景，碎成 29 段；给 10 就是一整块 247 点。

    **检测二：用文字气泡当锚点（粗阈值）**
      适合"照片类"图片。那张打印机照片有大片浅色（拍的白墙，248 左右），
      细阈值也会把它切碎（46/12/13/13 点）。但文字气泡的间隙是可靠的锚点。

    两种都跑，结果合并去重。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        log.warning("没装 numpy，跳过媒体区域检测（pip install numpy）")
        return []

    im = Image.open(img_path).convert("RGB")
    full_w, full_h = im.size
    if scale <= 0:
        scale = full_w / win_w if win_w else 1.0

    chat_left_pt = layout.norm_chat_left(win_w) * win_w
    top_pt = layout.title_h
    bot_pt = win_h - layout.input_h
    if bot_pt - top_pt < MEDIA_MIN_HEIGHT:
        return []

    px0 = int(chat_left_pt * scale)
    py_top = int(top_pt * scale)
    py_bot = int(bot_pt * scale)
    # 窗口最右几个点是边框/阴影，实测那一列内容占比 1.000，会把边界拉到窗口边缘
    px_end = min(full_w, int((win_w - WINDOW_EDGE_INSET) * scale))
    if px_end <= px0 or py_bot <= py_top:
        return []

    arr = np.asarray(im).astype(np.int16)[py_top:py_bot, px0:px_end]
    if arr.size == 0:
        return []

    # 背景色：取聊天区右上角一小块（一定是纯背景，不会压到消息）
    pby0, pby1 = 4, min(arr.shape[0], 40)
    pbx0 = int(arr.shape[1] * 0.6)
    if pby1 > pby0 and arr.shape[1] > pbx0:
        bg = np.median(arr[pby0:pby1, pbx0:].reshape(-1, 3), axis=0).astype(np.int16)
    else:
        bg = np.array([250, 250, 250], dtype=np.int16)

    # 每个检测器的结果分开收集。
    # ★ 为什么分开：同一个检测器内部需要合并（照片会被内部浅色区切碎），
    #   但**跨检测器不能合并** —— 实测把上下相邻的「物流轨迹截图」和
    #   「京东面单照片」并成了一条，视觉模型只能给出一段混合描述。
    groups: list[list[tuple[int, int, int, int]]] = []

    # ---- 检测一：细阈值按行扫（截图类） ----
    fine = _content_mask(arr, bg, _MEDIA_PIXEL_DIFF)
    g1: list[tuple[int, int, int, int]] = []
    for y0, y1 in _row_runs(fine, scale):
        box = _tighten_box(fine, y0, y1, px0, px_end, scale, MEDIA_MIN_HEIGHT)
        if box:
            g1.append(box)
    groups.append(g1)

    # ---- 检测二：文字气泡锚点（照片类） ----
    g2: list[tuple[int, int, int, int]] = []
    if text_boxes:
        rows = text_row_ranges(text_boxes, layout, win_w, win_h)
        edges = [top_pt] + [y for r in rows for y in r] + [bot_pt]
        coarse = _content_mask(arr, bg, _MEDIA_COARSE_DIFF)
        for i in range(0, len(edges) - 1, 2):
            g0, g1 = edges[i], edges[i + 1]
            if g1 - g0 < MEDIA_MIN_HEIGHT:
                continue
            y0 = max(0, int((g0 - top_pt) * scale))
            y1 = min(arr.shape[0], int((g1 - top_pt) * scale))
            if y1 <= y0:
                continue
            if coarse[y0:y1].mean() < 0.02:
                continue
            box = _tighten_box(coarse, y0, y1, px0, px_end, scale, MEDIA_MIN_HEIGHT)
            if box:
                g2.append(box)
    groups.append(g2)

    def _merge_same_group(items: list[tuple[int, int, int, int]]):
        out: list[tuple[int, int, int, int]] = []
        for b in sorted(items, key=lambda b: (b[1], b[0])):
            if out:
                m = out[-1]
                overlap_y = b[1] <= m[3] + MEDIA_MERGE_GAP * scale
                overlap_x = not (b[2] < m[0] - 20 or b[0] > m[2] + 20)
                if overlap_y and overlap_x:
                    out[-1] = (min(m[0], b[0]), min(m[1], b[1]),
                               max(m[2], b[2]), max(m[3], b[3]))
                    continue
            out.append(b)
        return out

    merged: list[tuple[int, int, int, int]] = []
    for g in groups:
        merged.extend(_merge_same_group(g))

    # 跨检测器只做去重：重叠面积大的算同一个，保留大的那个
    def _iou(a, b) -> float:
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        if inter <= 0:
            return 0.0
        ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
        return inter / ua if ua else 0.0

    dedup: list[tuple[int, int, int, int]] = []
    for b in sorted(merged, key=lambda b: -(b[2]-b[0])*(b[3]-b[1])):
        if any(_iou(b, d) > 0.6 for d in dedup):
            continue
        dedup.append(b)
    merged = [m for m in dedup if (m[3] - m[1]) / scale >= MEDIA_MIN_HEIGHT]

    out: list[MediaRegion] = []
    for x0, y0, x1, y1 in merged:
        fx0, fy0 = px0 + x0, py_top + y0
        fx1, fy1 = px0 + x1, py_top + y1
        center_pt = ((fx0 + fx1) / 2) / scale
        side = "in" if center_pt < (chat_left_pt + win_w) / 2 else "out"
        # 是不是视频：找叠加在缩略图**上面**的时长标记（如 "0:15"）。
        # ★ 必须要求它在图片区域内，不能只是"附近"。
        #   实测踩过：微信聊天里的**时间戳**（"21:10"）和视频时长格式完全一样，
        #   只要在图片附近就被误判成视频，结果把一张韵达面单说成"视频封面帧"。
        #   视频时长是画在缩略图左下角的，一定在区域内。
        kind = "image"
        if text_boxes:
            for b in text_boxes:
                if _DURATION_RE.match(b.text.strip()):
                    bx, by = b.cx * win_w, (1 - b.cy) * win_h
                    inside_x = (fx0 / scale - 6) < bx < (fx1 / scale + 6)
                    inside_y = (fy0 / scale - 6) < by < (fy1 / scale + 6)
                    if inside_x and inside_y:
                        kind = "video"
                        break
        out.append(MediaRegion(
            kind=kind, side=side,
            bbox=(fx0 / full_w, 1 - fy1 / full_h, fx1 / full_w, 1 - fy0 / full_h),
            top=1 - fy0 / full_h,
            pixel_box=(fx0, fy0, fx1, fy1),
        ))
    return out


# 视频封面上的时长标记，如 "0:15" / "1:02:33"
_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


# ======================================================================
# 语音消息检测与转写
# ======================================================================
#
# 为什么不能直接拿音频：微信本地语音文件是加密的（且我们不碰微信进程）。
# 好在**微信自带「转文字」功能** —— 语音气泡旁边就有这个按钮，点一下
# 微信自己把语音转成文字显示出来，我们再 OCR 读回来。
#
# 这样做的代价是：点击会改变界面状态（转写结果会留在屏幕上），
# 但这跟用户自己点一下「转文字」没区别，不会发出任何消息。
#
# 实测语音气泡的 OCR 形态（微信 4.1.13 macOS）：
#     '3"'      '3"（'      '• 3"'      '小 3"'      '• 3" • 转文字'
# 时长标记旁边的「转文字」按钮是我们点击的目标。

# 语音气泡上的时长标记："3"" / "• 3"（" / "12""
_VOICE_DUR_RE = re.compile(
    r"^[\u2022\u00b7\u3002\u5c0f\s]*(\d{1,3})\s*[" + _TIME_QUOTES + r"]"
    r"\s*[\uff08(]?[\u2022\u00b7\u3002\s]*$"      # 尾部也可能有圆点
)
_TRANSCRIBE_LABEL = "转文字"


@dataclass
class VoiceBubble:
    """一条语音消息在屏幕上的位置。"""

    side: str                    # in | out
    seconds: int                 # 时长（秒），读不到就是 0
    # 语音气泡本身的位置（像素，原点左上）
    pixel_box: tuple[int, int, int, int]
    # 「转文字」按钮的位置（像素中心，原点左上）；没有按钮就是 None
    button_xy: Optional[tuple[int, int]] = None
    # 气泡所在的 y（逻辑点），用来配对转写结果
    top_pt: float = 0.0


def find_voice_bubbles(text_boxes: list[TextBox], layout: Layout,
                       win_w: float, win_h: float) -> list[VoiceBubble]:
    """从 OCR 结果里找出语音气泡。

    靠时长标记（`3"`）定位：微信的语音气泡只画一个波形图标加时长，
    没有别的可识别文字。找到时长标记后，气泡就在它左（对方）或右（自己）。
    """
    chat_left = layout.norm_chat_left(win_w)
    y_low, y_high = layout.message_band(win_h)
    inside = [b for b in text_boxes
              if b.cx > chat_left and y_low < b.cy < y_high and b.text.strip()]

    durs: list[tuple[TextBox, int]] = []
    for b in inside:
        m = _VOICE_DUR_RE.match(b.text.strip())
        if m:
            durs.append((b, int(m.group(1))))
    if not durs:
        return []

    buttons = [b for b in inside if _TRANSCRIBE_LABEL in b.text]

    out: list[VoiceBubble] = []
    for b, secs in durs:
        a, c = b.cx - b.w / 2, b.cx + b.w / 2
        top_pt = (1 - (b.cy + b.h / 2)) * win_h
        bot_pt = (1 - (b.cy - b.h / 2)) * win_h
        # 时长标记在气泡里的位置：
        #   对方的气泡：波形在左、时长在右 → 气泡向左延展
        #   自己的气泡：时长在左、波形在右 → 气泡向右延展
        center = (a + c) / 2 * win_w
        side = "in" if center < (chat_left + win_w) / 2 else "out"
        if side == "in":
            bx0, bx1 = a * win_w - 90, c * win_w + 12
        else:
            bx0, bx1 = a * win_w - 12, c * win_w + 90

        # 找最近的「转文字」按钮（在气泡右边一点，或下一行）
        btn = None
        best = 1e9
        for t in buttons:
            tx, ty = t.cx * win_w, (1 - t.cy) * win_h
            if bx1 - 20 < tx < bx1 + 120 and top_pt - 30 < ty < bot_pt + 40:
                d = abs(tx - bx1) + abs(ty - top_pt)
                if d < best:
                    best, btn = d, (int(tx), int(ty))
        out.append(VoiceBubble(
            side=side, seconds=secs,
            pixel_box=(int(bx0), int(top_pt), int(bx1), int(bot_pt)),
            button_xy=btn, top_pt=top_pt,
        ))
    return out


# 语音气泡和它的转写结果之间的最大纵向距离（逻辑点）
VOICE_TRANSCRIPT_GAP = 70.0


def is_voice_msg(text: str) -> bool:
    """这条消息是不是语音。

    ★ 要按**归一后**的形态判断（`[语音 2秒]`），不能再用 _VOICE_DUR_RE
    去匹配原始 OCR 文本（`2"` / `• 2"`）—— parse_messages 已经把原始形态
    归一过了。实测踩过这个坑，导致转写结果没能合并回语音消息。
    """
    return (text or "").strip().startswith("[语音")


def merge_voice_transcripts(msgs: list[Observed], win_h: float,
                            gap_pt: float = VOICE_TRANSCRIPT_GAP) -> list[Observed]:
    """把微信自动转写出来的那条灰色气泡，合并回它对应的语音消息。

    微信开启"语音消息自动转文字"后，转写结果会作为**下一条气泡**显示：
    左对齐、紧跟语音气泡、没有单独的头像。实测形态（微信 4.1.13 macOS）：

        🔊 2"                    ← 语音气泡
        你们明天几点上班儿？       ← 转写结果，看起来像另一条消息

    不合并的话 AI 会以为商家发了**两条**消息（一条语音 + 一条文字）。
    """
    out: list[Observed] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if is_voice_msg(m.text) and not m.voice_text and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            # top 是归一化 y，越大越靠上；两条的间距换算成逻辑点
            gap = (m.top - nxt.top) * win_h
            if (nxt.side == m.side and 0 < gap <= gap_pt
                    and not nxt.media and not is_voice_msg(nxt.text)):
                m.voice_text = nxt.text.strip()
                out.append(m)
                i += 2
                continue
        out.append(m)
        i += 1
    return out
