"""Read-only dashboard and explicitly labelled saved examples."""
import json

from fastapi import APIRouter
from .config import COMPETITORS, PROJECT_ROOT

EXAMPLES = [
    ("floradomspb", "text", "text-floradomspb.json"),
    ("optflor", "text", "text-optflor.json"),
    ("floradomspb", "image", "image-floradomspb.json"),
]


def workspace_router(history):
    api = APIRouter()

    @api.get("/workspace")
    def workspace():
        items = []
        with history.connect() as connection:
            counts = dict(connection.execute(
                "SELECT kind, COUNT(*) FROM history WHERE status='completed' GROUP BY kind").fetchall())
            for slug, supplier in COMPETITORS.items():
                item = {"id": slug, **supplier, "notes": []}
                for kind in ["capture", "text", "image"]:
                    row = connection.execute(
                        "SELECT body FROM history WHERE competitor_id=? AND kind=? AND status='completed' "
                        "ORDER BY created_at DESC, id DESC LIMIT 1", (slug, kind)).fetchone()
                    item[kind] = json.loads(row[0]) if row else None
                items.append(item)
        return {"example": False, "items": items, "summary": {
            "competitors": len(items), "captures": counts.get("capture", 0),
            "analyses": counts.get("text", 0) + counts.get("image", 0),
        }}

    @api.get("/examples/workspace")
    def examples():
        items = {slug: {"id": slug, **supplier, "capture": None, "text": None, "image": None, "notes": []}
                 for slug, supplier in COMPETITORS.items()}
        records = []
        for slug, kind, filename in EXAMPLES:
            response = json.loads((PROJECT_ROOT / "examples/stage4" / filename).read_text(encoding="utf-8"))
            record = {
                "id": "example-" + slug + "-" + kind, "kind": kind, "competitor_id": slug,
                "created_at": response["source"]["captured_at"], "source": response["source"],
                "status": "completed", "error": None, "result": response, "artifacts": {},
                "example": True,
            }
            items[slug][kind] = record
            records.append(record)
        items["optflor"]["notes"] = ["В этом сохранённом ответе модель назвала цветы срезанными. Короткий исходный текст этого не подтверждает: требуется сверка с каталогом."]
        return {"example": True, "items": list(items.values()), "records": records,
                "summary": {"competitors": 5, "captures": 0, "analyses": len(records)}}

    return api
