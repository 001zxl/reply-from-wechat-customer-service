#!/usr/bin/env python3
"""模型切换工具。交付时对方用这个换模型，不用改代码。

    python bridge/switch_model.py              # 看现在有哪些模型、哪个能用
    python bridge/switch_model.py use deepseek-flash
    python bridge/switch_model.py check        # 体检：连一下看通不通
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "d": "\033[2m", "b": "\033[1m", "0": "\033[0m"}


def cmd_list() -> int:
    act = models.active_profile()
    print(f"\n{C['b']}模型列表{C['0']}")
    if act:
        print(f"当前使用：{C['g']}{act.label}{C['0']}  ({act.model})")
    else:
        print(f"当前使用：{C['r']}（没有可用模型）{C['0']}")
    print()

    for p in models.profiles():
        mark = f"{C['g']}●{C['0']}" if act and p.id == act.id else " "
        if p.configured:
            print(f" {mark} {C['b']}{p.id}{C['0']}  {p.label}")
            print(f"     模型 {p.model}   地址 {p.base_url}")
        else:
            print(f"   {C['d']}{p.id}  {p.label}{C['0']}")
            print(f"     {C['y']}还不可用：{'；'.join(p.problems)}{C['0']}")
        if p.note:
            print(f"     {C['d']}{p.note}{C['0']}")
        print()

    missing = [p for p in models.profiles() if not p.configured]
    if missing:
        print(f"{C['y']}要让上面某个模型可用，在 .env 里填对应的密钥：{C['0']}")
        seen = set()
        for p in missing:
            if p.api_key_env and p.api_key_env not in seen:
                seen.add(p.api_key_env)
                print(f"    {p.api_key_env}=你的密钥")
        print()
    print(f"{C['d']}切换：python bridge/switch_model.py use <id>{C['0']}\n")
    return 0


def cmd_use(profile_id: str) -> int:
    ok, msg = models.set_active(profile_id)
    print((f"{C['g']}✓{C['0']} " if ok else f"{C['r']}✗{C['0']} ") + msg)
    return 0 if ok else 1


async def cmd_check() -> int:
    """真连一下，确认密钥、地址、模型名都对。交付时先跑这个。"""
    act = models.active_profile()
    if act is None:
        print(f"{C['r']}没有可用模型。先在 .env 里填密钥。{C['0']}")
        return 1
    print(f"\n正在测试 {C['b']}{act.label}{C['0']}（{act.model}）…")
    try:
        from openai import AsyncOpenAI

        cli = AsyncOpenAI(api_key=act.api_key, base_url=act.base_url,
                          timeout=30.0, max_retries=0)
        resp = await cli.chat.completions.create(
            model=act.model,
            messages=[{"role": "user", "content": "只回复两个字：正常"}],
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        print(f"{C['g']}✓ 通了{C['0']}  返回：{text[:40]!r}")
        if usage:
            print(f"  {C['d']}token 用量：{usage.prompt_tokens} + {usage.completion_tokens}{C['0']}")
        return 0
    except Exception as exc:
        print(f"{C['r']}✗ 失败：{type(exc).__name__}: {str(exc)[:200]}{C['0']}")
        print(f"  {C['d']}挨个检查：① api_key_env 对应的密钥填了吗 "
              f"② base_url 对不对（北京/新加坡不一样）③ model 名和厂商文档一致吗{C['0']}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="模型切换")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["list", "use", "check"])
    ap.add_argument("value", nargs="?", default="")
    args = ap.parse_args()

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "use":
        if not args.value:
            print("用法：switch_model.py use <模型id>")
            return 1
        return cmd_use(args.value)
    return asyncio.run(cmd_check())


if __name__ == "__main__":
    raise SystemExit(main())
