"""白名单式的本机命令集成。

很多网点的"系统"其实是本地脚本 / Excel 宏 / 一个小 exe。
让模型自由拼命令是绝对不能做的，所以这里只认**配置里登记过的具名命令**：
模型只能说"我要跑 query_waybill"，具体跑什么由配置决定。

安全约束：
- 只跑 config 里登记的命令，参数按位置替换，不做 shell 解析（不用 shell=True）
- write 类动作在这里拒绝执行
- 有超时，超时按失败处理
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

# 从 argv 模板里抠出 {参数名}
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# 这些占位符由系统自己填，不算模型参数
RESERVED = {"python", "cwd", "env"}

from .base import ActionResult, ActionSpec


class CommandIntegration:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.system = str(cfg.get("id") or "")
        self.is_mock = bool(cfg.get("mock"))
        self.label = str(cfg.get("label") or self.system)
        self.description = str(cfg.get("description") or "")
        self.cwd = cfg.get("cwd") or None
        self._specs: dict[str, ActionSpec] = {}
        for c in cfg.get("commands", []):
            # 参数从 argv 模板里的 {xxx} 推出来。
            # 之前这里读的是不存在的 args 字段，导致工具定义里没有运单号参数，
            # 模型根本没法把单号传进来 —— 整个外部调用形同虚设。
            params = sorted({
                m for arg in c.get("argv", [])
                for m in PLACEHOLDER.findall(str(arg))
            } - RESERVED)
            spec = ActionSpec(
                system=self.system,
                name=str(c.get("name")),
                label=str(c.get("label") or c.get("name")),
                risk=c.get("risk", "read"),
                when=str(c.get("when") or ""),
                params=params,
                timeout=float(c.get("timeout", 20)),
            )
            self._specs[spec.name] = spec
            setattr(spec, "_raw", c)

    def actions(self) -> list[ActionSpec]:
        return list(self._specs.values())

    async def run(self, action: str, args: dict[str, Any]) -> ActionResult:
        spec = self._specs.get(action)
        if spec is None:
            return ActionResult(False, source=self.system,
                                error=f"{self.system} 没有名为 {action} 的命令")
        if spec.risk == "write":
            return ActionResult(
                False, source=self.system,
                error=f"「{spec.label}」有真实副作用，本系统不代执行，已转人工",
            )

        raw = getattr(spec, "_raw", {})
        argv = [str(x).format(**args) for x in raw.get("argv", [])]
        if not argv:
            return ActionResult(False, source=self.system, error="命令配置为空")

        env = dict(os.environ)
        for k, v in (raw.get("env") or {}).items():
            env[str(k)] = str(v)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout)
        except asyncio.TimeoutError:
            return ActionResult(False, source=self.system,
                                error=f"{self.label} 超过 {spec.timeout:.0f} 秒没返回")
        except FileNotFoundError:
            return ActionResult(False, source=self.system,
                                error=f"{self.label} 的命令不存在：{argv[0]}")
        except Exception as exc:
            return ActionResult(False, source=self.system,
                                error=f"{self.label} 执行失败：{type(exc).__name__}")

        if proc.returncode != 0:
            return ActionResult(
                False, source=self.system,
                error=f"{self.label} 退出码 {proc.returncode}：{err.decode('utf-8', 'ignore')[:200]}",
            )

        text = out.decode("utf-8", "ignore").strip()
        # 约定：命令的标准输出就是给模型看的事实
        return ActionResult(True, text=f"【{self.label}】\n{text[:1200]}", source=self.system)
