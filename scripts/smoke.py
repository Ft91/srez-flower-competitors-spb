"""Check a running server; --live explicitly enables two paid model requests."""
import argparse
import json
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding='utf-8')
parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true")
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "competitor-research-spb" / "data")
args = parser.parse_args()
with httpx.Client(base_url=args.base_url, timeout=190) as client:
    response = client.get("/health")
    response.raise_for_status()
    health = response.json()
    print(json.dumps(health, ensure_ascii=False, indent=2))
    if not args.live:
        print("Local health checked. No AI requests sent. Use --live for a text + image check.")
        raise SystemExit(0)
    if not health["api_key_configured"]:
        print("API key missing. Fill .env, restart the server, then repeat --live.")
        raise SystemExit(2)
    # Read both files before any paid request.
    page_path = args.data_dir / "floradomspb" / "home.json"
    image_path = args.data_dir / "floradomspb" / "home_view.png"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    image_data = image_path.read_bytes()
    text = page["text"]
    if not 10 <= len(text.strip()) <= 40000:
        raise SystemExit("Page text outside 10..40000 characters; select a suitable source explicitly.")
    source_metadata = {'source_name': page.get('title') or 'ФлораДомСПБ', 'source_url': page['url']}
    if page.get('captured_at_utc'):
        source_metadata['captured_at'] = page['captured_at_utc']
    text_response = client.post("/analyze_text", json={
        "text": text, **source_metadata,

    })
    print("TEXT", text_response.status_code, text_response.text)
    if text_response.is_error:
        raise SystemExit(1)
    image_response = client.post("/analyze_image", files={"file": (image_path.name, image_data, "image/png")}, data=source_metadata)
    print("IMAGE", image_response.status_code, image_response.text)
    raise SystemExit(1 if image_response.is_error else 0)
