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

# 额度用完（finish_reason=length）时的降级提示词。
# 实测：详细提示词要 7667 token（思考 10665 字），给 6000 必爆。
# 降级到这句只需要 3700 左右，先拿到内容再说，总比空着强。
FALLBACK_PROMPT = (
    "这是快递网点客服收到的图片。简要回答："
    "1) 这是什么？2) 运单号是多少？3) 物流状态？"
    "4) 收寄件人和地址？看不清就说看不清，不要猜。"
)

# 给视觉模型的提问。要点：
#   · 明确它的角色（快递网点客服助手），不要说成通用图片描述
#   · 强制它区分"业务信息"和"无关内容"
#   · 强制它对看不清的部分诚实
#   · 禁止它猜商家意图（那是下一步客服模型的事，要基于完整上下文）
VISION_PROMPT = """你是快递网点的客服助手。商家发来一张{kind_cn}。

# 铁律：只描述你**真实看到**的字，一个字都不许补

真实事故（必须避免）：面单上有个分拣码写着「3-LR-九龙 6-F4」，
寄件地址写的是「山东省潍坊市坊子区北海路…」。
模型把分拣码里的「九龙」抠出来，跟地址里的「坊子区」拼在一起，
输出成「山东省潍坊市坊子区**九龙街道**」——
**这个地址面单上根本不存在，是编出来的**，而且看起来完全合理，
不逐字核对根本发现不了。

所以：
- **不许补全**。看到「九龙」就写「九龙」，不许写成「九龙街道」「九龙镇」。
- **不许展开缩写**。看到「潍」就写「潍」，不许写成「潍坊」。
- **不许把不同区域的文字拼在一起**。分拣码归分拣码，地址归地址。
- **不许用常识填空**。不知道某个区属哪个市，就不要补。
- 某个字段看不到，就写「未见」，**不许猜、不许留个像样的值**。
- 数字（运单号、电话、金额、重量）**逐个字符抄**，抄不全就写
  「只看到前 N 位：xxx」，不许给出一个"补全后"的号码。
- **不要把"印刷在商品/设备上的文字"当成客户信息**。面单照片里常常还有
  商品标签、设备铭牌，上面的型号、规格、电压、**制造商地址**都不是寄收件信息。
  举例：标签写着「制造商：佳能公司，日本国东京都大田区…」——
  那是**厂商地址**，不是收件地址，要明确说明。
- **不要推测商家想让你做什么**。你只负责把图里的内容如实读出来，
  商家要办什么事由后面结合聊天上下文判断。

# 快递面单的结构（认位置，别认错区）

1. **顶部**：快递公司 logo 和名称、运单号条形码、运单号明文
2. **寄件信息区**：标着「寄」字 → 寄件人姓名、电话、详细地址
3. **收件信息区**：标着「收」字 → 收件人姓名、电话、详细地址
4. **物流信息区**：运单号、产品类型、重量、代收货款、件数
5. **分拣码区（最容易认错的地方）**：形如 `3-LR-九龙 6-F4` 或 `630H092-3070`
   - 这是**内部路由编码**，三段/四段分别代表：
     分拨中心 / 网点或分部 / 派送段或业务员码
   - **里面的地名是"哪个网点负责"，不是收件人所在地，不是地址的一部分**
   - 这类码要单独归到「分拣路由信息」，**绝对不能写进收件地址**
6. **底部**：备注、签收栏、广告

不同快递公司版面略有差异，但「寄/收」两个大字和运单号条码一定在最显眼的位置。

# 隐私面单（现在很常见，别当成"看不清"）

- 收件人姓名可能只留姓：`徐*` 或 `徐**` → 要如实写出**徐**，不要只说"被遮挡"
- 收件人电话显示为手机号后四位：`*******7428` → 这是**尾号**，不是完整号码
- 寄件人电话可能是**虚拟号**：`18413225798转7117` → 要标明这是虚拟号

# 输出格式

**1. 这是什么**：一句话说清是面单照片、物流轨迹截图、聊天截图还是别的。

**2. 逐字抄录**：把你能看清的文字，按它**在图上出现的位置**分组抄下来。
每组先用一句话说明它在面单的哪个区（如「寄件信息区」「分拣码区」）。
看不清的字用 `?` 代替，例如 `山东省潍坊市坊子区北海??`。

**3. 归纳字段**（只在上面抄录确实包含时才写，没有就写「未见」）：
   - 运单号 / 快递公司
   - 寄件人：姓名 / 电话 / 地址
   - 收件人：姓名 / 电话 / 地址
   - 货物：名称 / 重量 / 件数 / 代收货款
   - 分拣路由信息（单独列，不要混进地址）
   - 时间、状态

**4. 看不清的部分**：明确列出哪些字看不清。

用中文，不要客套话。**宁可写「未见」，也不要给一个看起来对的答案。**
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

        # 思考模型的额度要给足：实测详细提问需要 7667 token
        budget = max(prof.max_tokens, 3000)
        client = AsyncOpenAI(api_key=prof.api_key, base_url=prof.base_url,
                             timeout=300.0, max_retries=1)
        b64 = base64.b64encode(data).decode()

        # ★ 看图抄字必须关掉思考。
        # 实测同一张面单：思考开启 47 秒 / 11808 token（还容易把额度耗光、
        # 返回空字符串）；关掉后 5 秒 / 1042 token，而且**准确度更高** ——
        # 文字抄录不需要推理，思考反而让模型"脑补"：它曾把分拣码里的
        # 「九龙」跟地址里的「坊子区」拼成「九龙街道」，面单上根本没这个地址。
        extra: dict = {}
        if prof.thinking == "disabled":
            extra["thinking"] = {"type": "disabled"}
        elif prof.thinking == "enabled":
            extra["thinking"] = {"type": "enabled"}

        resp = await client.chat.completions.create(
            model=prof.model,
            extra_body=extra or None,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            # 注意：思考模式下 temperature 不生效（API 会忽略）
            temperature=prof.temperature,
            max_tokens=budget,
        )
        choice = resp.choices[0]
        raw = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)

        # 额度被思考吃光时，用简短提示词再试一次 —— 总比返回空强
        if not raw and getattr(choice, "finish_reason", "") == "length":
            log.warning("视觉模型额度用尽（model=%s, max_tokens=%s），改用简短提示词重试",
                        prof.model, budget)
            try:
                resp = await client.chat.completions.create(
                    model=prof.model,
                    extra_body=extra or None,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": FALLBACK_PROMPT},
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        ],
                    }],
                    temperature=prof.temperature,
                    max_tokens=budget,
                )
                choice = resp.choices[0]
                raw = (choice.message.content or "").strip()
                usage = getattr(resp, "usage", None)
                if raw:
                    log.info("降级重试成功（%d 字）", len(raw))
            except Exception:
                log.exception("降级重试也失败")

        if not raw:
            reason = getattr(choice, "finish_reason", "?")
            log.warning("视觉模型返回空（finish_reason=%s，model=%s，max_tokens=%s）",
                        reason, prof.model, budget)
            return MediaResult(
                ok=False, crop_path=str(path),
                error=f"视觉模型返回了空内容（finish_reason={reason}）。"
                      f"多半是 max_tokens 不够，被思考过程吃光了。"
                      f"现在 config/models.json 里 {prof.id} 的 max_tokens 是 {budget}，"
                      f"可以再调大些。",
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
