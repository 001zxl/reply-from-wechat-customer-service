"""外部审查的 HTTP 冒烟（6 项）：走 ASGI TestClient，不开真端口、不碰微信。

来源：审查报告附带的 review_smoke.py。放在这里当回归闸 ——
审核台、鉴权、mock 出草稿、接管状态这几条链路改坏了要立刻发现。
"""

import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent.parent   # 仓库根（本文件在 tests/ 下）
os.chdir(ROOT)
import sys; sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory(prefix="wechat-http-review-") as directory:
    os.environ.update(DB_PATH=str(Path(directory) / "smoke.db"),
                      WECHAT_CHANNEL="mock", LLM_BACKEND="mock", DRY_RUN="1",
                      LOGISTICS_PROVIDER="mock", APP_TOKEN="offline-review-token")
    from fastapi.testclient import TestClient
    from app.server import app

    results = []
    def check(name, passed, detail):
        results.append({"name": name, "passed": bool(passed), "detail": detail})

    with TestClient(app) as client:
        response = client.get("/health")
        check("health", response.status_code == 200 and response.json()["channel"] == "mock", response.json())
        response = client.get("/desk")
        check("desk HTML served", response.status_code == 200 and "text/html" in response.headers["content-type"], response.status_code)
        response = client.get("/api/desk/queue")
        check("unauthenticated API rejected", response.status_code == 401, response.status_code)
        headers = {"Authorization": "Bearer offline-review-token"}
        response = client.post("/api/desk/compose", headers=headers, json={
            "conversation_id": "offline-http-review", "text": "这边发货怎么收费？",
            "title": "Offline synthetic conversation"})
        check("mock compose returns draft", response.status_code == 200 and response.json().get("ok"), response.json())
        response = client.get("/api/desk/queue", headers=headers)
        items = response.json().get("items", [])
        check("draft available in review queue", response.status_code == 200 and len(items) == 1, {"count": len(items)})
        response = client.post("/api/desk/takeover", headers=headers, json={
            "conversation_id": "offline-http-review", "minutes": 30})
        response = client.get("/api/desk/conversations", headers=headers)
        check("takeover stored", response.json()["items"][0]["taken_over"], response.json()["items"][0]["taken_over"])
    result = {"offline_only": True, "checks": results, "passed": sum(x["passed"] for x in results)}
    (Path(os.environ.get("REVIEW_OUT", str(ROOT / "review_smoke_results.json")))).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert all(x["passed"] for x in results)
