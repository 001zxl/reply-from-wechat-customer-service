#!/usr/bin/env python3
"""翻阅微信会话的历史消息（**只读，绝不发送**）。

用途：把某个群/单聊的历史记录一路往上翻，OCR 下来拼成一份完整的时间线，
存成 JSONL/文本，供人阅读或喂给模型学习。

为什么不复用 `read_messages`：那个函数是给"实时收新消息"用的，只读**当前
可见的一屏**，而且开头会把聊天区滚到底。翻历史要做的是相反的方向 ——
反复向上滚、每滚一次抓一屏、再把多屏拼成一条时间线。

怎么拼（这里踩过坑，别改成"按消息条数对齐"）
------------------------------------------------
第一版是按**消息条数**对齐的：找最大的 k 让新一屏的末尾 k 条与已累积内容
的开头 k 条逐条相似。结果 40 屏里有 29 屏报"无重叠"，看起来像漏了一半内容。

实测证明那是**误报**：用像素互相关量相邻两屏的真实位移，40 屏**每一屏都
精确位移 360px**（聊天区高 856px），也就是重叠了 58%，一条都没漏。
按条数对不上的原因是 —— **图片被屏幕上下边缘切断时，同一张图在两屏里会被
切成条数不同、高度不同的碎片**，序列自然对不齐。

所以现在改成：
  1. 用像素互相关算出每一屏相对上一屏滚了多少（`pixel_delta`）；
  2. 把每条消息换算成一个**全局内容坐标** `abs_y`（越大越新）；
  3. 按 `abs_y` 去重合并（文字按位置+模糊文本，图片按矩形重叠）。

这样跨屏的同一张图能靠坐标对上，不再需要"条数刚好相等"这种脆弱假设。

安全设计（三重）：
  1. 本文件里**没有任何发送调用**，不 import 发送路径。
  2. 会话必须先在 `bridge/guard.py` 的白名单里，否则 open_conversation 直接抛。
  3. `watch` 传空列表，通道不会走轮询。

用法：
    .venv/bin/python tools/read_chat_history.py --chat "某个群名" --pages 40
    .venv/bin/python tools/read_chat_history.py --chat "某某群" --pages 120 --scroll 2
    .venv/bin/python tools/read_chat_history.py --chat "某某群" --dry-run   # 只抓一屏
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.macos_vision import MacWeChatVisionChannel  # noqa: E402
from adapters.vision_common import (  # noqa: E402
    _is_system_line,
    _similar,
    parse_messages,
    title_ok,
)
from bridge import screen  # noqa: E402
from bridge.vision_ocr import ocr_image  # noqa: E402

log = logging.getLogger("history")

OUT_DIR = ROOT / "data" / "history"

# 同一条消息在两屏之间允许的位置抖动（截图内的像素）。
# 依据：微信消息块之间的间距 > 70px，给 16px 不会把两条不同的消息并成一条。
MATCH_TOL_PX = 16.0

# 媒体区块跨屏合并时，纵向重叠超过较小者的这个比例就算同一张图
MEDIA_OVERLAP_RATIO = 0.40


# ---------------------------------------------------------------- 一屏的解析

def _click_latest_pill(png: str, win) -> bool:
    """点微信自带的「N条新消息 / 回到最新」药丸，一步跳到最新消息。

    为什么值得单独做：靠滚轮从"翻上去几万像素"的位置滚回底部，要滚几十次，
    而且中途任何一次滚轮没生效都会让"到底了"的判断提前成立 —— 实测因此丢过
    一整段（15:46 之后的全部消息）。点这个药丸是微信自己提供的"回到最新"，
    一次到位。点的是聊天区里的浮层，不会发出任何消息。
    """
    try:
        boxes = ocr_image(png)
    except Exception:
        return False
    for b in boxes:
        t = b.text.strip()
        if "条新消息" not in t and "回到最新" not in t and "以下为新消息" not in t:
            continue
        x = win.x + b.cx * win.w
        y = win.y + (1 - b.cy) * win.h
        if not (win.x < x < win.x + win.w and win.y < y < win.y + win.h):
            continue
        screen.click(x, y)
        return True
    return False


def split_sender(text: str, sender: str) -> tuple[str, str]:
    """把群聊里贴在气泡上方的发言人名字从正文里拆出来。

    微信 Mac 的群里，发言人名字是气泡上方一行灰色小字，和气泡的间距很小，
    所以 `parse_messages` 会把它和正文聚成同一块，文本长这样：
        "某商家客服\\n773123456789012，770123456789012，拦截"
    只有字数和标点都像"名字"时才拆，避免把真正的短消息（"好的"）当成人名。
    """
    if sender:
        return text, sender
    first, sep, rest = text.partition("\n")
    if (sep and rest.strip() and len(first.strip()) <= 16
            and not first.strip().endswith(("。", "！", "？", "!", "?", "：", ":", "，", ","))):
        # 注意：**不能**因为首行含 "@" 就不拆。
        # 群里有人的昵称里就带 @（比如「<网点>商家服务<姓名>-查件请@我」），
        # 按"含@就当正文"会把发言人当成消息内容。
        # 靠字数就够了：带 @ 的正文（"@某客服 773123456789012 已登记"）一行远超 16 字。
        return rest.strip(), first.strip()
    return text, ""


def extract_page(ch, png: str, win, region: tuple[int, int, int, int]) -> tuple[str, list[dict]]:
    """把一屏截图解析成条目列表，每条都带截图内的像素坐标。

    kind：
      · msg   —— 一条文字消息（side=in/out，群里可能有 sender）
      · sys   —— 日期/时间分隔线，例如 "9月25日 19:00"
      · media —— 图片/视频（顺手裁出来存盘）

    这里复刻 `_read_messages` 的解析顺序（**先找媒体再解析文字**），
    因为商家发来的图片里常有印刷字，OCR 会把它读成一条假消息。
    """
    rx0, ry0, rx1, ry1 = region
    boxes = ocr_image(png)
    title = ch._title_from(boxes, win)

    try:
        scale = screen.image_size(png)[0] / win.w if win.w else 2.0
    except Exception:
        scale = 2.0
    full_h = win.h * scale

    # 媒体区域（图片/视频）。crop 存盘但不调模型。
    media = ch._read_media(png, boxes, win)
    kept = boxes
    if media:
        def _in_media(b) -> bool:
            px, py = b.cx * win.w * scale, (1 - b.cy) * win.h * scale
            for r in media:
                x0, y0, x1, y1 = r.media_box
                if x0 - 6 <= px <= x1 + 6 and y0 - 6 <= py <= y1 + 6:
                    return True
            return False

        kept = [b for b in boxes if not _in_media(b)]

    msgs = parse_messages(kept, ch.layout, win.w, win.h)
    if media:
        msgs = sorted(msgs + media, key=lambda m: -m.top)

    items: list[dict] = []
    for m in msgs:
        text, sender = split_sender(m.text, m.sender)
        if m.media:
            x0, y0, x1, y1 = m.media_box
        else:
            x0, x1 = 0, int(win.w * scale)
            y0 = y1 = int((1 - m.top) * full_h)
            y1 = y0 + 1
        items.append({
            "kind": "media" if m.media else "msg",
            "side": m.side,
            "sender": sender,
            "text": text,
            "media": m.media,
            "media_path": m.media_path,
            # 相对"互相关区域"的坐标（这才是跨屏可比的那套坐标）
            "y0": float(y0 - ry0),
            "y1": float(y1 - ry0),
            "x0": float(x0 - rx0),
            "x1": float(x1 - rx0),
        })

    # 日期/时间分隔线。parse_messages 会把它们当噪音丢掉，
    # 但翻历史必须知道每条消息是哪天的，所以单独捡回来。
    chat_left = ch.layout.norm_chat_left(win.w)
    y_low, y_high = ch.layout.message_band(win.h)
    for b in kept:
        if b.cx <= chat_left or not (y_low < b.cy < y_high):
            continue
        t = b.text.strip()
        if not t or not _is_system_line(t):
            continue
        # 界面浮层（"26条新消息"/"回到最新"）也算系统行，但它不是时间分隔线，丢掉
        if any(k in t for k in ("条新消息", "回到最新", "以下新消息", "以下是新消息")):
            continue
        yy = (1 - b.top) * full_h
        items.append({
            "kind": "sys", "side": "", "sender": "", "text": t,
            "media": "", "media_path": "",
            "y0": float(yy - ry0), "y1": float(yy - ry0) + 1,
            "x0": 0.0, "x1": 0.0,
        })

    items.sort(key=lambda x: x["y0"])
    return title, items


# ---------------------------------------------------------------- 像素互相关

def pixel_delta(prev_png: str, cur_png: str, region: tuple[int, int, int, int]) -> Optional[int]:
    """算这一屏相对上一屏**向下移动了多少像素**（= 向上滚了多少）。

    向上翻时内容整体往下走，所以位移为正。用灰度块的均方差找最佳位移，
    先 1/4 分辨率粗搜、再原分辨率细化 —— 直接用原图算 200 个位移太慢。

    返回 None 表示算不出来（图读不了），调用方要走兜底。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    rx0, ry0, rx1, ry1 = region
    try:
        a = np.asarray(Image.open(prev_png).convert("L"), dtype=np.float32)[ry0:ry1, rx0:rx1]
        b = np.asarray(Image.open(cur_png).convert("L"), dtype=np.float32)[ry0:ry1, rx0:rx1]
    except Exception:
        log.exception("读图失败，无法算滚动位移")
        return None
    H = a.shape[0]
    if H < 120 or b.shape[0] != H:
        return None

    def shift_pairs(arr_a, arr_b, d: int):
        """取位移 d 下可比的两段。

        d > 0：b 里的内容比 a 往下走了 d 行（= 向上滚了 d）→ 比 a[0:H-d] 与 b[d:H]
        d < 0：反过来比 a[-d:H] 与 b[0:H+d]
        """
        n = arr_a.shape[0]
        if d >= 0:
            return arr_a[0:n - d], arr_b[d:n]
        return arr_a[-d:n], arr_b[0:n + d]

    def score(d: int) -> float:
        """位移 d 下的均方差（越小越像）。"""
        if abs(d) >= H - 80:
            return float("inf")
        x, y = shift_pairs(a, b, d)
        if x.shape[0] < 80 or x.shape != y.shape:
            return float("inf")
        return float(np.mean((x - y) ** 2))

    small = 4
    a_s = a[::small, ::small]
    b_s = b[::small, ::small]
    Hs = a_s.shape[0]
    best_d, best_s = 0, float("inf")
    max_d_s = max(2, Hs - max(6, Hs // 6))
    for ds in range(-10, max_d_s):
        x, y = shift_pairs(a_s, b_s, ds)
        if x.shape[0] < 20 or x.shape != y.shape:
            continue
        s = float(np.mean((x - y) ** 2))
        if s < best_s:
            best_s, best_d = s, ds * small

    best = (float("inf"), best_d)
    for d in range(best_d - small * 2, best_d + small * 2 + 1):
        s = score(d)
        if s < best[0]:
            best = (s, d)
    return best[1]


# ---------------------------------------------------------------- 去重合并

_AHASH_CACHE: dict[str, Optional[int]] = {}


def _ahash(path: str) -> Optional[int]:
    """图片的 8x8 均值哈希（感知哈希），用来判断"这两块是不是同一张图"。

    ★ 为什么不能用文件 md5：同一张图在两次截屏里会差一两个像素
      （滚动位置、抗锯齿、重采样），md5 立刻就不同了。
      不过跨屏的同一张图更常见的是"被屏幕边缘切成不同高度"，
      那种情况靠 `_ahash` 也救不了，得靠矩形重叠判断（见 `_merge_records`）。
    """
    if not path:
        return None
    if path in _AHASH_CACHE:
        return _AHASH_CACHE[path]
    val: Optional[int] = None
    try:
        from PIL import Image
        im = Image.open(path).convert("L").resize((8, 8), Image.LANCZOS)
        px = list(im.getdata())
        avg = sum(px) / len(px)
        val = 0
        for i, p in enumerate(px):
            if p >= avg:
                val |= 1 << i
    except Exception:
        val = None
    _AHASH_CACHE[path] = val
    return val


def _text_same(a: dict, b: dict) -> bool:
    if a["kind"] != b["kind"] or a.get("side") != b.get("side"):
        return False
    return _similar(a["text"].strip(), b["text"].strip())


def _media_same(a: dict, b: dict) -> bool:
    """两块媒体算不算同一张图：矩形纵向明显重叠 + 种类一致。"""
    if a.get("media") != b.get("media"):
        return False
    ov = min(a["abs_y1"], b["abs_y1"]) - max(a["abs_y0"], b["abs_y0"])
    if ov <= 0:
        return False
    shorter = min(a["abs_y1"] - a["abs_y0"], b["abs_y1"] - b["abs_y0"])
    if shorter <= 0:
        return False
    if ov / shorter < MEDIA_OVERLAP_RATIO:
        return False
    # 横向也要沾边，免得把左右两张并排的图并起来
    return not (a["abs_x1"] < b["abs_x0"] - 20 or b["abs_x1"] < a["abs_x0"] - 20)


def _taller(a: dict, b: dict) -> dict:
    return a if (a["abs_y1"] - a["abs_y0"]) >= (b["abs_y1"] - b["abs_y0"]) else b


def _merge_records(records: list[dict]) -> list[dict]:
    """按全局坐标去重，合并成一条完整时间线（返回按 abs_y 升序 = 从旧到新）。

    文字：位置接近 + 文本模糊相同 → 同一条。
    媒体：矩形重叠够多 → 同一张，保留**高的那个**（高的那个是没被屏幕边缘
          切断的完整裁图）。

    为什么要"位置接近"这个条件：微信里两条一模一样的短消息（连着两个"好的"）
    是完全可能的，只按文本去重会把它们并成一条，凭空少一条。
    加上位置条件就不会 —— 它们的 y 差着几十像素。
    """
    records = sorted(records, key=lambda r: (r["abs_y0"], r["abs_y1"]))
    out: list[dict] = []
    for r in records:
        hit = None
        for prev in reversed(out[-8:]):
            if abs(prev["abs_y0"] - r["abs_y0"]) > MATCH_TOL_PX:
                continue
            if r["kind"] == "media":
                if _media_same(prev, r):
                    hit = prev
                    break
            elif _text_same(prev, r):
                hit = prev
                break
        if hit is None:
            out.append(dict(r))
            continue
        if r["kind"] == "media":
            win_ = _taller(hit, r)
            hit["abs_y0"], hit["abs_y1"] = min(hit["abs_y0"], r["abs_y0"]), max(hit["abs_y1"], r["abs_y1"])
            hit["abs_x0"], hit["abs_x1"] = min(hit["abs_x0"], r["abs_x0"]), max(hit["abs_x1"], r["abs_x1"])
            if win_ is r:                       # 新的这块更完整，采用它的图和文本
                hit["media_path"] = r["media_path"]
                hit["text"] = r["text"]
        else:
            if not hit.get("sender") and r.get("sender"):
                hit["sender"] = r["sender"]
    return sorted(out, key=lambda r: r["abs_y0"])


def attach_stray_senders(items: list[dict]) -> int:
    """把"孤零零一行发言人名字"并回它下面那条消息。

    微信群里发言人名字和气泡之间偶尔会有一点空隙，`parse_messages` 就把它
    判成独立的一块了，于是正文里多出一行 `[对方] <网点>商家服务<姓名>-查件请@我`。
    这里用**已经识别出来的发言人名字集合**当白名单来认它，认出来就附着到
    后面那条消息上。

    返回合并掉的行数。
    """
    names = {e["sender"].strip() for e in items if e.get("sender")}
    if not names:
        return 0
    merged = 0
    out: list[dict] = []
    i = 0
    while i < len(items):
        e = items[i]
        t = (e.get("text") or "").strip()
        is_stray = (
            e["kind"] == "msg" and not e.get("sender") and "\n" not in t
            and 2 <= len(t) <= 16 and not t.endswith(("。", "！", "？", "!", "?", "：", ":"))
            and i + 1 < len(items)
            and items[i + 1]["side"] == e["side"]
            and (t in names or any(_similar(t, n, 0.85) for n in names))
        )
        if is_stray:
            nxt = items[i + 1]
            if not nxt.get("sender"):
                nxt["sender"] = t
            merged += 1
            i += 1
            continue
        out.append(e)
        i += 1
    items[:] = out
    return merged


# ---------------------------------------------------------------- 主流程

def scrape(chat: str, pages: int, scroll_clicks: int,
           out_dir: Path, dry_run: bool = False) -> tuple[list[dict], dict]:
    ch = MacWeChatVisionChannel(watch=[])       # watch 为空 = 不会走轮询/发送
    # 注意：这里**不走** ch.read_messages()，因为那个函数开头会把聊天区滚到底，
    # 正是翻历史要避免的。extract_page 直接调它下面的解析函数，不碰 settings。

    if not ch.open_conversation(chat):
        raise RuntimeError(f"打不开会话：{chat}（不在白名单里？先在 bridge/guard.py allow-chat）")

    win = ch.main_window()
    try:
        scale = screen.image_size(
            str(screen.capture_window(win.window_id, str(out_dir / "_probe.png")))
        )[0] / win.w
    except Exception:
        scale = 2.0
    # 互相关/坐标统一的区域：聊天区（去掉标题栏和输入框、去掉右侧边框）
    region = (
        int(ch.layout.norm_chat_left(win.w) * win.w * scale) + 4,
        int(ch.layout.title_h * scale) + 4,
        int((win.w - 14) * scale),
        int((win.h - ch.layout.input_h) * scale) - 4,
    )

    shot_dir = out_dir / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    cx = win.x + int(ch.layout.chat_left(win.w)
                     + (win.w - ch.layout.chat_left(win.w)) * 0.5)
    cy = win.y + int(win.h * 0.42)

    # ★ 先滚到**真正**的最新一屏，再往上翻。
    #
    #   三个坑叠在一起，每一个都实测踩过：
    #   1. 打开会话时聊天区不一定停在最新处（这个群一打开就浮着
    #      「N条新消息」的药丸，说明之前有人翻过历史）。
    #   2. 通道自带的 `scroll_chat_to_bottom` 只滚固定 18 格 —— 上一次抓取
    #      把历史翻上去四万像素之后，18 格**根本滚不回来**。实测：第二轮抓取
    #      第一屏停在 15:46，最新的 15:46~21:11 整段丢掉，而且不报错。
    #   3. "连续两屏画面一样就算到底"会被**某一轮滚轮没生效**骗到。实测第二次
    #      抓取只滚了 8 轮就宣布到底，其实还差 4 万像素。必须连续 3 次不变才算。
    #
    #   所以：先试着点「N条新消息」药丸（那是微信自带的"回到最新"，一步到位），
    #   点不到再滚，而且要连续 3 轮不动才算到底。
    if not dry_run:
        try:
            print("▸ 先滚到最新一屏…")
            ch.focus()
            time.sleep(0.3)
            _probe = shot_dir / "_bottom.png"
            screen.capture_window(win.window_id, _probe)
            if _click_latest_pill(_probe, win):
                print("  （点了「N条新消息」，直接跳到最新）")
                time.sleep(1.2)
            prev_h = ""
            still = 0
            for rnd in range(1, 81):
                screen.capture_window(win.window_id, _probe)
                h = hashlib.md5(_probe.read_bytes()).hexdigest()
                if h == prev_h:
                    still += 1
                    if still >= 3:
                        print(f"  （滚了 {rnd - 1} 轮到底）")
                        break
                else:
                    still = 0
                prev_h = h
                screen.scroll(cx, cy, clicks=-25)
                time.sleep(0.7)
            else:
                print("  ⚠ 滚了 80 轮画面还在变，可能没到底 —— 最新的消息有丢的风险")
            time.sleep(0.5)
        except Exception as exc:
            print(f"  （滚到底失败，按当前可见位置开始：{exc}）")

    records: list[dict] = []
    stats = {"pages": 0, "gaps": 0, "titles": [], "deltas": [], "jumps": 0}
    stalled = 0
    total_up = 0.0        # 累计向上滚了多少像素
    prev_png: Optional[str] = None

    for i in range(1, pages + 1):
        png = str(shot_dir / f"page_{i:03d}.png")
        ch.focus()
        time.sleep(0.35)
        screen.capture_window(win.window_id, png)

        title, page = extract_page(ch, png, win, region)
        stats["pages"] = i
        if title and title not in stats["titles"]:
            stats["titles"].append(title)

        if not title_ok(title, chat):
            print(f"  第 {i:3d} 屏：当前打开的是 {title!r}，不是目标会话 → 停（人可能在用微信）")
            break

        note = ""
        if prev_png is not None:
            d = pixel_delta(prev_png, png, region)
            if d is None:
                note = "位移算不出来，沿用上一次的步长"
                d = int(stats["deltas"][-1]) if stats["deltas"] else int(scroll_clicks * 180)
            else:
                if d < 0:
                    stats["jumps"] += 1
                stats["deltas"].append(d)
                region_h = region[3] - region[1]
                if d >= region_h - 40:
                    stats["gaps"] += 1
                    note = f"⚠ 位移 {d}px 已超过一屏（{region_h}px），中间可能漏内容"
                elif abs(d) < 4:
                    stalled += 1
                    note = f"⚠ 画面没动（第 {stalled} 次）"
                else:
                    stalled = 0
                    note = f"位移 {d}px"
            # ★ 位移的正负都要累加进去。
            #   推导没假设 d > 0：内容在页 i 的 y 处、页 i+1 的 y+d 处，
            #   要求 abs 相同 → T_{i+1} = T_i + d。d 为负（画面往下跳）时
            #   同样成立，硬要"重置基准"反而会把后面的坐标全算错。
            total_up += d

        # ★ 全局坐标：abs_y = 屏内 y − 累计上滚量。
        #   符号别写反。要求"同一内容在两屏里算出同一个 abs_y"：
        #     页 i  的内容在 y，页 i+1 里它下移到了 y+d
        #     y - T_i = (y + d) - T_{i+1}   →   T_{i+1} = T_i + d
        #   所以 `total_up` 是**累加** d，而 abs_y 是**减** total_up。
        #   写反的后果不是"稍微错位"，而是同一内容在两屏里差 2d 像素，
        #   跨屏永远匹配不上，去重全废（实测 254 条只并掉几十条）。
        #   另外这样定下来，abs_y 越大越新 → 升序排列就是"从旧到新"。
        for e in page:
            r = dict(e)
            r["abs_y0"] = e["y0"] - total_up
            r["abs_y1"] = e["y1"] - total_up
            r["abs_x0"] = e["x0"]
            r["abs_x1"] = e["x1"]
            records.append(r)

        print(f"  第 {i:3d} 屏：本屏 {len(page):3d} 条 | {note or '首页'} | 累计原始 {len(records):5d} 条")

        # ★ 连续滚不动才收工，而且中间要给微信时间加载更早的历史。
        #   踩过的坑：滚到"当前已加载的那一段"的头时，微信会去读更早的记录，
        #   这段时间里的滚轮事件被**直接忽略**（位移 0）。第一版把"位移 0"
        #   当成"到顶了"就退出，结果只抓到 16:23 就停了，而实际上还有
        #   15:38 之前的记录 —— 而且不报错，看起来像正常结束。
        if stalled >= 5:
            print(f"  连续 {stalled} 屏画面不动 → 判定已到最早（或微信不再加载更早的记录）")
            break
        if dry_run:
            break

        prev_png = png
        screen.scroll(cx, cy, clicks=abs(scroll_clicks))
        # 卡住的时候多等一会儿再滚，给它读盘/联网的时间
        time.sleep(2.5 if stalled else 0.9)

    items = _merge_records(records)
    merged = attach_stray_senders(items)
    stats["stray_senders"] = merged
    stats["raw"] = len(records)

    # 把裁图挪到 media/ 下面，别散在 shots 里
    for it in items:
        p = it.get("media_path") or ""
        if not p:
            continue
        src = Path(p)
        dst = media_dir / src.name
        if src.exists() and src.parent != media_dir:
            try:
                dst.write_bytes(src.read_bytes())
                it["media_path"] = str(dst)
            except Exception:
                pass

    stats["chars"] = sum(len(x["text"]) for x in items)
    return items, stats


def write_outputs(chat: str, items: list[dict], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in chat if c not in r'/\:*?"<>|').strip() or "chat"
    jsonl = out_dir / f"{safe}.jsonl"
    text = out_dir / f"{safe}.txt"

    with jsonl.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    with text.open("w", encoding="utf-8") as f:
        f.write(f"# {chat} 历史记录（从旧到新）\n\n")
        for it in items:
            if it["kind"] == "sys":
                f.write(f"\n───── {it['text']} ─────\n")
            elif it["kind"] == "media":
                who = "我方" if it["side"] == "out" else (it["sender"] or "对方")
                f.write(f"[{who}] {'[图片]' if it['media'] == 'image' else '[视频]'}"
                        f"  <{Path(it['media_path']).name if it['media_path'] else ''}>\n")
            else:
                who = "我方" if it["side"] == "out" else (it["sender"] or "对方")
                body = it["text"].replace("\n", " ⏎ ")
                f.write(f"[{who}] {body}\n")
    return jsonl, text


def main() -> int:
    ap = argparse.ArgumentParser(description="翻阅微信会话历史（只读）")
    ap.add_argument("--chat", required=True, help="会话名，必须已在 guard 白名单里")
    ap.add_argument("--pages", type=int, default=40, help="最多翻几屏（默认 40）")
    ap.add_argument(
        "--scroll", type=int, default=2,
        help=("每屏向上滚几格（默认 2）。实测 880x640 窗口下每 2 格 = 360px 位移，"
              "聊天区高 856px，重叠 58%%，很安全。调大会减少屏数但重叠变少，"
              "滚到 6 格以上就超过一屏了，中间会漏内容"))
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    ap.add_argument("--dry-run", action="store_true", help="只抓当前一屏，不滚动")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    out_dir = Path(args.out)
    print(f"▸ 目标会话：{args.chat}")
    print(f"▸ 最多 {args.pages} 屏，每屏上滚 {args.scroll} 格")
    print("▸ 只读模式：本脚本没有任何发送调用\n")

    items, stats = scrape(args.chat, args.pages, args.scroll, out_dir, args.dry_run)
    jsonl, text = write_outputs(args.chat, items, out_dir)

    n_msg = sum(1 for x in items if x["kind"] == "msg")
    n_media = sum(1 for x in items if x["kind"] == "media")
    n_sys = sum(1 for x in items if x["kind"] == "sys")
    deltas = stats.get("deltas") or []
    print("\n=== 结果 ===")
    print(f"翻屏数    {stats['pages']}")
    print(f"原始条目  {stats.get('raw', 0)}  →  去重后 {len(items)}")
    print(f"消息      文字 {n_msg} 条 / 图片视频 {n_media} 条 / 日期分隔 {n_sys} 条")
    print(f"总字数    {stats['chars']}")
    if deltas:
        print(f"每屏位移  {min(deltas)}~{max(deltas)}px（中位 {sorted(deltas)[len(deltas)//2]}px）")
    print(f"漏内容告警 {stats['gaps']} 处（位移超过一屏）")
    print(f"画面回跳   {stats['jumps']} 次（人在动微信，坐标基准已重置）")
    print(f"并回发言人名字 {stats.get('stray_senders', 0)} 行")
    print(f"JSONL     {jsonl}")
    print(f"可读文本  {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
