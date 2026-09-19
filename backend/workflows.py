import base64
import hashlib
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .config import COMPETITORS, CompetitorId
from .evidence import price_checks
from .schemas import AnalysisResponse, EvidenceCheck, SourceInfo, StrictModel, TextAnalysisRequest
from .service import AnalysisError, prepare_image



class CollectRequest(StrictModel):
    competitor_ids: list[CompetitorId] = Field(default_factory=lambda: list(COMPETITORS), min_length=1, max_length=5)


class CaptureAnalysisRequest(StrictModel):
    modes: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text", "image"], min_length=1, max_length=2)


class DemoRequest(CaptureAnalysisRequest):
    competitor_ids: list[CompetitorId] = Field(default_factory=lambda: ["floradomspb"], min_length=1, max_length=5)


class Workflows:
    def __init__(self, settings, analyzer, history, parser):
        self.settings, self.analyzer, self.history, self.parser = settings, analyzer, history, parser

    async def analyze(self, kind, content, source, *, parent=None, notes=None, competitor_id=None):
        record = self.history.new(kind, source=source.model_dump(mode="json"),
                                  parent_id=parent["id"] if parent else None,
                                  competitor_id=parent["competitor_id"] if parent else competitor_id)
        self.history.save(record)
        try:
            if kind == "text":
                self.history.put_file(record, "input.txt", content.encode("utf-8"), "text/plain")
                result, check = await self.analyzer.analyze_text(content)
            else:
                data = base64.b64decode(content.split(",", 1)[1])
                is_png = content.startswith("data:image/png;")
                self.history.put_file(record, "input.png" if is_png else "input.jpg", data,
                                      "image/png" if is_png else "image/jpeg")
                result = await self.analyzer.analyze_image(content)
                check = EvidenceCheck(mode="visual_review_required", quotes_checked=0,
                    note="Распознавание изображения и субъективные оценки требуют ручной проверки.")
            response = AnalysisResponse(
                input_type=kind, source=source, model=self.settings.model_for(kind), analysis=result,
                evidence_check=check, price_checks=price_checks(result) if kind == "text" else [],
                history_id=record["id"], input_notes=notes or [],
            )
            self.history.finish(record, result=response.model_dump(mode="json"))
            return response
        except AnalysisError as error:
            self.history.finish(record, error={"code": error.code, "message": error.message})
            raise

    async def collect(self, competitor_ids):
        results = []
        for competitor_id in dict.fromkeys(competitor_ids):
            item = COMPETITORS[competitor_id]
            record = self.history.new("capture", competitor_id=competitor_id,
                                      source={"name": item["name"], "url": item["url"], "captured_at": None})
            self.history.save(record)
            try:
                meta, files = await run_in_threadpool(self.parser.collect, competitor_id)
                for name, (data, media) in files.items():
                    self.history.put_file(record, name, data, media)
                record["source"].update(url=meta["url"], captured_at=meta["captured_at"])
                self.history.finish(record, result=meta)
            except AnalysisError as error:
                self.history.finish(record, error={"code": error.code, "message": error.message})
            results.append(record)
        return results

    async def analyze_capture(self, record_id, modes):
        capture = self.history.get(record_id)
        if not capture or capture["kind"] != "capture":
            raise AnalysisError(404, "capture_not_found", "Такого снимка страницы нет в истории.")
        if capture["status"] != "completed" or not capture["result"]["usable_for_analysis"]:
            raise AnalysisError(409, "capture_not_usable", "Этот снимок не готов к анализу. Проверьте результат сбора.")
        source = SourceInfo.model_validate(capture["source"])
        results = []
        for kind in dict.fromkeys(modes):
            name = "page.txt" if kind == "text" else "viewport.png"
            artifact = self.history.file(record_id, name)
            if artifact is None:
                raise AnalysisError(404, "artifact_missing", "Исходный материал отсутствует в папке истории.")
            notes = []
            try:
                material = artifact[0].read_bytes()
                if hashlib.sha256(material).hexdigest() != capture["artifacts"][name]["sha256"]:
                    raise AnalysisError(409, "artifact_changed", "Исходный файл изменился после сбора. Создайте новый снимок.")
                if kind == "text":
                    text = material.decode("utf-8")
                    if len(text) > 40000:
                        text = text[:40000]
                        notes.append("На анализ переданы только первые 40 000 символов. Полный текст сохранён в снимке страницы.")
                    content = TextAnalysisRequest(text=text).text
                else:
                    content = await run_in_threadpool(prepare_image, material)
                    notes.append("Анализируется первый экран 1440×900. Полный скриншот сохранён отдельно.")
                result = await self.analyze(kind, content, source, parent=capture, notes=notes)
                results.append({"kind": kind, "success": True, "result": result.model_dump(mode="json")})
            except AnalysisError as error:
                results.append({"kind": kind, "success": False, "error": {"code": error.code, "message": error.message}})
        return results


def router(workflows):
    api = APIRouter()

    @api.get("/competitors")
    def competitors():
        return {"items": [{"id": key, **value} for key, value in COMPETITORS.items()]}

    @api.post("/collect")
    async def collect(body: CollectRequest):
        records = await workflows.collect(body.competitor_ids)
        return {"success": all(r["status"] == "completed" for r in records), "items": records}

    @api.post("/captures/{record_id}/analyze")
    async def analyze_capture(record_id: UUID, body: CaptureAnalysisRequest):
        results = await workflows.analyze_capture(record_id.hex, body.modes)
        return {"success": all(r["success"] for r in results), "items": results}

    @api.post("/parsedemo")
    async def parsedemo(body: DemoRequest):
        captures = await workflows.collect(body.competitor_ids)
        results = []
        for capture in captures:
            analyses, error = [], None
            if capture["status"] == "completed":
                try:
                    analyses = await workflows.analyze_capture(capture["id"], body.modes)
                except AnalysisError as exc:
                    error = {"code": exc.code, "message": exc.message}
            results.append({"capture": capture, "analyses": analyses, "error": error})
        return {"success": all(r["capture"]["status"] == "completed" and not r["error"]
                               and all(a["success"] for a in r["analyses"]) for r in results),
                "items": results}

    @api.get("/history")
    def history(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
                competitor_id: CompetitorId | None = None, kind: Literal["capture", "text", "image"] | None = None):
        return workflows.history.list(limit, offset, competitor_id=competitor_id, kind=kind)

    @api.get("/history/{record_id}")
    def detail(record_id: UUID):
        record = workflows.history.get(record_id.hex)
        if not record:
            raise AnalysisError(404, "history_not_found", "Запись не найдена.")
        return record

    @api.get("/history/{record_id}/files/{name}")
    def file(record_id: UUID, name: str):
        artifact = workflows.history.file(record_id.hex, name)
        if artifact is None:
            raise AnalysisError(404, "artifact_missing", "Файл не найден.")
        path, media = artifact
        return FileResponse(path, media_type=media, headers={"X-Content-Type-Options": "nosniff"})

    return api
