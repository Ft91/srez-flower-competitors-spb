"""Run the Selenium demo against an already running local server."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")
parser = argparse.ArgumentParser(description="Сбор сайта через Selenium и сохранение истории.")
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--competitors", nargs="+", default=["floradomspb"],
                    choices=["7flowers", "optflor", "floradomspb", "florografia", "tsvetomania"])
parser.add_argument("--collect-only", action="store_true", help="Только сбор, без платных запросов ИИ.")
args = parser.parse_args()
endpoint = "/collect" if args.collect_only else "/parsedemo"
try:
    with httpx.Client(timeout=1200, trust_env=False) as client:
        response = client.post(args.base_url.rstrip("/") + endpoint,
                               json={"competitor_ids": args.competitors})
        response.raise_for_status()
        result = response.json()
except (httpx.HTTPError, ValueError) as exc:
    print(f"Запрос не выполнен ({type(exc).__name__}). Проверьте, запущен ли start.ps1.")
    raise SystemExit(1)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
output = Path(__file__).resolve().parents[1] / "runtime" / "demo-reports" / f"{stamp}.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("Результат сохранён:", output)
print("Успешно:", result["success"])
for item in result["items"]:
    capture = item if args.collect_only else item["capture"]
    print(capture["competitor_id"], capture["status"], capture["id"])
    if capture["error"]:
        print("  Ошибка:", capture["error"]["message"])
    if not args.collect_only:
        for analysis in item["analyses"]:
            print(" ", analysis["kind"], "готово" if analysis["success"] else analysis["error"]["message"])
        if item["error"]:
            print(" ", item["error"]["message"])
raise SystemExit(0 if result["success"] else 2)
