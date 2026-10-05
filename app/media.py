"""媒体消息理解 —— 把商家发来的图片/视频封面裁出来，送多模态模型看懂。

背景
----
OCR 只能读文字。商家发来的面单截图、实物照片、视频封面：

    OCR 读到的：'Canon ssanan 35'  '220V'  'JWP67400'   ← 碎片，看不懂
    视觉模型看到的：'一台佳能多功能打印机的背面铭牌，标签可见型号
                    JWP67400、220V 50Hz。没有快递信息，标签上的地址
                    是制造商地址不是收件地址。'

差的不是准确率，是**理解**。所以流程是：

    截屏 → 检测媒体区域（vision_common.find_media_regions）
         → 裁出图片 → 送 deepseek-v4-flash-vision-exp → 拿到描述
         → 作为这条消息的内容注入对话

踩过的坑
--------
1. **这是思考模型，max_tokens 必须给足。** 给少了会被思考过程吃光，
   返回空字符串而且**不报错**（finish_reason=length，content 是空的）。
   实测：
     · 简单提问（"这是什么颜色"）  150 token 够
     · 打印机铭牌照片             1824 token
     · 物流轨迹截图 + 面单照片     4044 token   ← 内容复杂时暴增
   **耗量跟提示词的复杂度直接相关** —— 同一个模型、同一张图，
   换一句更细的提问，token 用量能从 380 涨到 4044。
   所以 config/models.json 里给到 6000。

2. **模型名不在 /v1/models 列表里。** `deepseek-v4-flash-vision-exp` 是
   实验性模型，接口不列它，但实际能调通。别用列表判断可用性。

3. **图片按 token 计费，一张图最多 384 token**，价格与 Flash 一致。
   但每次调用都有开销，所以做了内容哈希缓存，同一张图只分析一次。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import models
from .config import ROOT

log = logging.getLogger("media")

MEDIA_DIR = ROOT / "data" / "media"
CACHE_FILE = ROOT / "data" / "media_cache.json"
CACHE_LIMIT = 500                    # 缓存条目上限，超了丢最旧的

# 给视觉模型的提问。要点：
#   · 明确它的角色（快递网点客服助手），不要说成通用图片描述
#   · 强制它区分"业务信息"和"无关内容"
#   · 强制它对看不清的部分诚实
#   · 禁止它猜商家意图（那是下一步客服模型的事，要基于完整上下文）
VISION_PROMPT = """你是快递网点的客服助手。商家发来一张{kind_cn}，请如实描述。

请回答三件事：
1. **这是什么**：拍的是什么、截的是什么页面。一句话。
2. **和快递业务有关的信息**：把能看清的都列出来 ——
   运单号（通常是 12~15 位数字，或字母+数字）、收寄件人、电话、地址、
   货物名称、重量、代收货款、日期、网点名、状态文字。
   如果是聊天截图或订单页截图，把其中的关键文字也列出来。
3. **看不清的部分**：明确说哪些地方看不清、无法确认。

要求：
- 只描述你**真实看到**的。看不清就说看不清，**绝对不要猜**。
- 不要把图片里的印刷文字（型号、规格、电压、制造商地址这类）当成
  客户信息。如果图里的地址是厂商地址而不是收件地址，要说明。
- **不要推测商家想让你做什么**，只描述图片内容。
- 用中文，简洁，不要客套话。
"""


@dataclass
class MediaResult:
    ok: bool
    text: str = ""                   # 注入给客服模型的描述
    raw: str = ""                    # 视觉模型原始输出
    seconds: float = 0.0
    cached: bool = False
    error: str = ""
    crop_path: str = ""
    meta: dict = field(default_factory=dict)


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    if len(cache) > CACHE_LIMIT:
        # 按写入时间丢最旧的
        ordered = sorted(cache.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)
        cache = dict(ordered[:CACHE_LIMIT])
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _hash_image(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def crop_region(screenshot: str | Path, pixel_box: tuple[int, int, int, int],
                tag: str = "media") -> tuple[Path, bytes]:
    """从截图里裁出媒体区域，存到 data/media/ 并返回 (路径, 字节)。"""
    from PIL import Image

    im = Image.open(screenshot).convert("RGB")
    x0, y0, x1, y1 = pixel_box
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(im.width, int(x1)), min(im.height, int(y1))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"裁切框无效：{pixel_box}（图 {im.width}x{im.height}）")

    crop = im.crop((x0, y0, x1, y1))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    import io

    buf = io.BytesIO()
    crop.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()

    path = MEDIA_DIR / f"{tag}_{_hash_image(data)[:12]}.png"
    if not path.exists():
        path.write_bytes(data)
    return path, data


async def describe_media(screenshot: str | Path,
                         pixel_box: tuple[int, int, int, int],
                         kind: str = "image",
                         conversation: str = "",
                         use_cache: bool = True) -> MediaResult:
    """裁图 + 送视觉模型 + 拿描述。同一张图只分析一次。"""
    t0 = time.time()
    try:
        if pixel_box and any(pixel_box):        # 常规：从截图里裁
            path, data = crop_region(screenshot, pixel_box, tag=kind)
        else:                                    # 已经是裁好的图，直接读
            path = Path(screenshot)
            data = path.read_bytes()
    except Exception as exc:
        log.exception("取图失败")
        return MediaResult(ok=False, error=f"取图失败：{exc}")

    digest = _hash_image(data)

    if use_cache:
        hit = _load_cache().get(digest)
        if hit and hit.get("raw"):
            return MediaResult(
                ok=True, text=hit.get("text", ""), raw=hit["raw"],
                seconds=time.time() - t0, cached=True,
                crop_path=str(path), meta={"hash": digest},
            )

    prof = models.vision_profile()
    if prof is None:
        return MediaResult(
            ok=False, crop_path=str(path),
            error="没有可用的视觉模型。请在 config/models.json 里配一个 "
                  "多模态档案（见 deepseek-vision 那条），并填好对应密钥。",
        )

    kind_cn = {"image": "图片", "video": "视频（只能看到封面帧）"}.get(kind, "图片")
    prompt = VISION_PROMPT.format(kind_cn=kind_cn)

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=prof.api_key, base_url=prof.base_url,
                             timeout=90.0, max_retries=1)
        b64 = base64.b64encode(data).decode()
        resp = await client.chat.completions.create(
            model=prof.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            temperature=prof.temperature,
            # ★ 思考模型：给少了会被思考过程吃光，返回空且不报错
            max_tokens=max(prof.max_tokens, 2500),
        )
        choice = resp.choices[0]
        raw = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)

        if not raw:
            reason = getattr(choice, "finish_reason", "?")
            log.warning("视觉模型返回空（finish_reason=%s，model=%s）", reason, prof.model)
            return MediaResult(
                ok=False, crop_path=str(path),
                error=f"视觉模型返回了空内容（finish_reason={reason}）。"
                      f"多半是 max_tokens 不够，被思考过程吃光了，"
                      f"把 config/models.json 里 {prof.id} 的 max_tokens 调大。",
            )

        text = _to_message_text(raw, kind)
        if use_cache:
            cache = _load_cache()
            cache[digest] = {"raw": raw, "text": text, "at": time.time(),
                             "conv": conversation, "model": prof.model}
            _save_cache(cache)

        return MediaResult(
            ok=True, text=text, raw=raw, seconds=time.time() - t0,
            crop_path=str(path),
            meta={"hash": digest, "model": prof.model,
                  "tokens": (usage.prompt_tokens + usage.completion_tokens) if usage else None},
        )
    except Exception as exc:
        log.exception("视觉模型调用失败")
        return MediaResult(ok=False, crop_path=str(path),
                           error=f"{type(exc).__name__}: {str(exc)[:200]}",
                           seconds=time.time() - t0)


def _to_message_text(raw: str, kind: str) -> str:
    """把视觉描述包成一条"消息内容"，让客服模型知道这是图片不是商家打的字。"""
    head = "[商家发来一张图片]" if kind == "image" else "[商家发来一段视频，以下是封面帧]"
    body = raw.strip().replace("\n\n", "\n")
    tail = ("\n（以上是系统对图片内容的识别结果。图片里可能还有识别不到的内容，"
            "涉及运单号等关键信息时，如果和商家文字说的不一致，以商家文字为准。）")
    return f"{head}\n{body}{tail}"


def cache_stats() -> dict:
    cache = _load_cache()
    return {"entries": len(cache), "file": str(CACHE_FILE)}
