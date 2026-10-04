#!/usr/bin/env python3
"""演示用的「网点内部系统查询」。

真实场景把这个换成你自己的脚本 / exe 就行，只要满足一条约定：
**把结果打到标准输出，那就是给 AI 看的事实。**

可以用任何语言写，CommandIntegration 只认 stdio。
"""

from __future__ import annotations

import argparse
import json
import sys

# 假装这是网点内部系统的数据
DB = {
    "773123456789012": {
        "业务员": "张伟",
        "归属网点": "杭州余杭一部",
        "重量": "1.2kg",
        "代收货款": "无",
        "备注": "客户要求放前台，别打电话",
    },
    "773987654321098": {
        "业务员": "李娜",
        "归属网点": "南京建邺二部",
        "重量": "0.6kg",
        "代收货款": "到付 29 元",
        "备注": "工作日送公司，周末送家里",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--waybill", required=True)
    args = ap.parse_args()

    info = DB.get(args.waybill)
    if not info:
        # 查不到就明确说查不到，不要编
        print(json.dumps(
            {"found": False, "waybill": args.waybill, "message": "内部系统里没有这个单号"},
            ensure_ascii=False,
        ))
        return 0

    print(json.dumps(
        {"found": True, "waybill": args.waybill, **info},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
