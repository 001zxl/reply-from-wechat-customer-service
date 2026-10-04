#!/usr/bin/env python3
"""打包 Windows/Linux 测试版。

    python deploy/make_package.py

为什么要用 Python 而不是 `zip` 命令：macOS 的 zip 写中文文件名时**不设
UTF-8 标志位**（bit 11），Windows 解压出来全是乱码。Python 的 zipfile
会自动设，Windows 10+ 的资源管理器能正确显示。

包里不含：.env（真实密钥）、data/（聊天记录）、.venv/、.ssh-relay/、
dist/、__pycache__。打进包的是 .env.example，所有密钥字段都是空的。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
TOP = "微信客服助手"          # 解压后的一级目录，别把文件散到人家桌面

INCLUDE = [
    "app", "adapters", "bridge", "integrations", "logistics",
    "config", "tools", "tests", "deploy", "docs",
    "requirements.txt", "run.sh", "README.md", "AGENTS.md",
    "LICENSE", ".env.example", ".gitignore",
]

# 这些一律不打包
SKIP_DIRS = {"__pycache__", ".venv", "data", ".ssh-relay", "dist", ".git", ".pytest_cache"}
SKIP_FILES = {".env", ".DS_Store", "Thumbs.db", "diagnose-report.txt"}
SKIP_SUFFIX = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log"}


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.name in SKIP_FILES:
        return True
    if path.suffix in SKIP_SUFFIX:
        return True
    if path.name.startswith(".") and path.name not in (".env.example", ".gitignore"):
        return True
    return False


def main() -> int:
    DIST.mkdir(exist_ok=True)
    out = DIST / "微信客服助手-朋友测试版.zip"
    if out.exists():
        out.unlink()

    added = 0
    skipped = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in INCLUDE:
            src = ROOT / item
            if not src.exists():
                print(f"  [警告] 找不到 {item}，跳过")
                continue
            if src.is_file():
                if should_skip(src):
                    skipped += 1
                    continue
                z.write(src, f"{TOP}/{item}")
                added += 1
                continue
            for f in sorted(src.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(ROOT)
                if should_skip(rel):
                    skipped += 1
                    continue
                z.write(f, f"{TOP}/{rel.as_posix()}")
                added += 1

    size = out.stat().st_size
    print(f"\n  打包完成：{out}")
    print(f"  大小    : {size/1024/1024:.2f} MB")
    print(f"  文件数  : {added}")
    print(f"  跳过    : {skipped}")

    # ---- 自检 ----
    print("\n  自检：")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        nonascii = [i for i in z.infolist() if any(ord(c) > 127 for c in i.filename)]
        flag_ok = all(i.flag_bits & 0x800 for i in nonascii) if nonascii else True
        print(f"    中文文件名       : {len(nonascii)} 个，"
              f"UTF-8 标志位 {'✓ 全部已设' if flag_ok else '✗ 有遗漏，Windows 会乱码'}")

        leaked = [n for n in names if n.endswith("/.env") or "/data/" in n
                  or ".ssh-relay" in n or n.endswith(".db")]
        print(f"    敏感文件         : {'✓ 无' if not leaked else '✗ ' + str(leaked)}")

        keys = []
        for n in names:
            if n.endswith((".py", ".md", ".json", ".txt", ".bat", ".ps1", ".example", ".sh")):
                try:
                    body = z.read(n).decode("utf-8", "ignore")
                except Exception:
                    continue
                for line in body.splitlines():
                    if "sk-" in line and "sk-xxx" not in line and "sk-在这里" not in line:
                        s = line.split("sk-", 1)[1].split()[0].strip('"\'`,)')
                        if len(s) > 20:
                            keys.append(f"{n}: sk-{s[:8]}...")
        print(f"    明文密钥         : {'✓ 无' if not keys else '✗ ' + str(keys[:3])}")

        for need in ["AGENTS.md", "docs/Windows验证交接说明.md",
                     "adapters/windows_vision.py",
                     "bridge/inspect_wechat_windows.py",
                     "deploy/windows/1-install.bat"]:
            ok = f"{TOP}/{need}" in names
            print(f"    含 {need:42s} {'✓' if ok else '✗'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
