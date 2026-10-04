#!/usr/bin/env python3
"""Windows 微信通道的诊断工具 —— 上生产前先跑这个，逐项确认。

这个脚本**只读，不发送任何消息**，也不会点击会话列表。

    python bridge\\inspect_wechat_windows.py --check       # 检查环境和依赖
    python bridge\\inspect_wechat_windows.py --window      # 找微信窗口，打印坐标
    python bridge\\inspect_wechat_windows.py --shot        # 截图，看能不能抓到
    python bridge\\inspect_wechat_windows.py --ocr         # 截图 + OCR，看中文识别
    python bridge\\inspect_wechat_windows.py --layout      # 在截图上画出版面假定区域
    python bridge\\inspect_wechat_windows.py --list        # 扫描会话列表（只读名字）

按顺序跑一遍，每一项都对了再上生产。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(r"C:\Windows\Temp\wechat_cs_probe")
OUT.mkdir(parents=True, exist_ok=True)


def cmd_check() -> int:
    import platform

    print("\n=== 1. 运行环境 ===")
    print(f"  系统      : {platform.system()} {platform.release()}")
    if platform.system() != "Windows":
        print("  ✗ 这不是 Windows，后面的检查没法做")
        return 1

    print("\n=== 2. 依赖 ===")
    deps = {
        "pyautogui": "截图与鼠标键盘",
        "win32gui": "窗口定位（pywin32）",
        "win32clipboard": "剪贴板（pywin32）",
        "PIL": "图像处理（Pillow）",
        "rapidocr_onnxruntime": "中文 OCR",
    }
    missing = []
    for mod, why in deps.items():
        try:
            __import__(mod)
            print(f"  ✓ {mod:24s} {why}")
        except ImportError:
            print(f"  ✗ {mod:24s} {why}  ← 缺这个")
            missing.append(mod)
    if missing:
        print("\n  安装缺的包：")
        print("    pip install pyautogui pywin32 pillow rapidocr-onnxruntime")
        return 1

    print("\n=== 3. 微信进程 ===")
    try:
        from adapters.windows_vision import WindowsWeChatVisionChannel

        ch = WindowsWeChatVisionChannel(shot_dir=OUT)
        hwnd, rect = ch._find_window()
        print(f"  ✓ 找到窗口 句柄={hwnd}  位置=({rect[0]},{rect[1]})  尺寸={rect[2]}x{rect[3]}")
    except Exception as exc:
        print(f"  ✗ {type(exc).__name__}: {exc}")
        print("     确认微信已登录、窗口没有最小化")
        return 1
    return 0


def _channel():
    from adapters.windows_vision import WindowsWeChatVisionChannel

    return WindowsWeChatVisionChannel(shot_dir=OUT)


def cmd_window() -> int:
    ch = _channel()
    hwnd, rect = ch._find_window()
    x, y, w, h = rect
    print(f"\n微信主窗口")
    print(f"  句柄  : {hwnd}")
    print(f"  左上角: ({x}, {y})")
    print(f"  尺寸  : {w} x {h}")
    print(f"\n版面假定值（窗口内像素）：")
    print(f"  图标栏宽   : {ch.layout.rail_w}")
    print(f"  会话列表宽 : {ch.layout.list_w}")
    print(f"  标题栏高   : {ch.layout.title_h}")
    print(f"  输入区高   : {ch.layout.input_h}")
    print(f"  聊天区左界 : {ch.layout.chat_left(w)}")
    print(f"  输入框中心 : {ch.layout.input_center(w, h)}")
    print(f"  发送按钮   : {ch.layout.send_button(w, h)}")
    print("\n用 --layout 在截图上画出来对一下，不对就调 Layout 的数值。")
    return 0


def cmd_shot() -> int:
    ch = _channel()
    ch.focus()
    rect, path = ch._capture("probe")
    print(f"\n截图已保存：{path}")
    print(f"  窗口尺寸: {rect[2]} x {rect[3]}")
    print("\n打开这张图看看：")
    print("  1. 是不是完整的微信窗口（没被别的窗口盖住）")
    print("  2. 左侧会话列表、右侧聊天区都在")
    return 0


def cmd_ocr() -> int:
    ch = _channel()
    ch.focus()
    rect, path = ch._capture("probe")
    boxes = ch._ocr(path)
    print(f"\nOCR 识别到 {len(boxes)} 个文本块")
    print(f"  截图: {path}")
    print("\n前 20 条（归一化坐标，原点左下）：")
    for b in sorted(boxes, key=lambda b: -b.cy)[:20]:
        print(f"  y={b.cy:.3f} x={b.x:.3f}  {b.text[:60]!r}")
    if not boxes:
        print("\n  ✗ 一个字都没识别出来。检查截图是不是空的、或者 OCR 装错了")
        return 1
    print("\n中文识别正常的话，这一步就过了。")
    return 0


def cmd_layout() -> int:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("需要 Pillow")
        return 1
    ch = _channel()
    ch.focus()
    rect, path = ch._capture("probe")
    w, h = rect[2], rect[3]
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)

    chat_left = ch.layout.chat_left(w)
    # 竖向分区
    d.line([(ch.layout.rail_w, 0), (ch.layout.rail_w, h)], fill=(255, 0, 0), width=2)
    d.line([(chat_left, 0), (chat_left, h)], fill=(255, 0, 0), width=2)
    # 横向分区
    d.line([(chat_left, ch.layout.title_h), (w, ch.layout.title_h)], fill=(0, 128, 255), width=2)
    d.line([(chat_left, h - ch.layout.input_h), (w, h - ch.layout.input_h)],
           fill=(0, 128, 255), width=2)
    # 输入框和发送按钮
    ix, iy = ch.layout.input_center(w, h)
    d.ellipse([ix - 8, iy - 8, ix + 8, iy + 8], outline=(0, 200, 0), width=3)
    bx, by = ch.layout.send_button(w, h)
    d.ellipse([bx - 10, by - 10, bx + 10, by + 10], outline=(255, 0, 255), width=3)

    outp = str(OUT / "layout.png")
    im.save(outp)
    print(f"\n已画出假定区域：{outp}")
    print("  红色竖线 = 图标栏 / 会话列表 的边界")
    print("  蓝色横线 = 标题栏底 / 输入区顶")
    print("  绿圈     = 输入框点击位置")
    print("  紫圈     = 发送按钮位置")
    print("\n对照这张图：")
    print("  · 红竖线要正好落在会话列表和聊天区之间")
    print("  · 蓝横线要正好在标题栏下面、输入框上面")
    print("  · 绿圈要落在输入框里、紫圈要落在「发送」按钮上")
    print("  哪个不对就改 adapters/vision_common.py 里 DEFAULT_LAYOUT 的对应数值")
    return 0


def cmd_list() -> int:
    ch = _channel()
    ch.focus()
    names = ch.list_conversations()
    print(f"\n扫描到 {len(names)} 个会话（只读名字，没有打开任何会话）：")
    for n in names:
        print(f"  · {n}")
    if not names:
        print("\n  ✗ 一个都没扫到。检查版面标定（先跑 --layout）")
        return 1
    print("\n确认名字认得对，就可以用这些名字配白名单了。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for f, h in [("check", "检查环境和依赖"), ("window", "找微信窗口"),
                 ("shot", "截图"), ("ocr", "OCR 识别测试"),
                 ("layout", "在截图上画版面，用于标定"),
                 ("list", "扫描会话列表")]:
        g.add_argument(f"--{f}", action="store_true", help=h)
    args = ap.parse_args()
    for name in ("check", "window", "shot", "ocr", "layout", "list"):
        if getattr(args, name):
            return globals()[f"cmd_{name}"]()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
