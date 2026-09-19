from contextlib import asynccontextmanager
import sqlite3
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile
from pydantic import AwareDatetime, HttpUrl
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, PROJECT_ROOT, CompetitorId
from .workspace import workspace_router
from .history import HistoryStore
from .parsing_service import ParsingService
from .workflows import Workflows, router
from .schemas import AnalysisResponse, SourceInfo, TextAnalysisRequest
from .service import AnalysisError, AnalysisService, MAX_IMAGE_BYTES, prepare_image


def create_app(settings: Settings | None = None, service: AnalysisService | None = None, parser=None) -> FastAPI:
    settings = settings or Settings()
    analyzer = service or AnalysisService(settings)
    history = HistoryStore(settings.runtime_dir / "history")
    workflows = Workflows(settings, analyzer, history, parser or ParsingService(settings))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await analyzer.close()

    app = FastAPI(
        title="Анализ конкурентов — опт цветов, СПб",
        description="Интерфейс, сохранённые материалы, анализ и сравнение поставщиков.",
        version="0.6.0", lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.exception_handler(AnalysisError)
    async def analysis_error_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={
            "success": False, "error": {"code": exc.code, "message": exc.message},
        })

    @app.exception_handler(OSError)
    @app.exception_handler(sqlite3.Error)
    async def storage_error_handler(request, exc):
        return JSONResponse(status_code=507, content={
            "success": False, "error": {"code": "storage_error",
                "message": "Не удалось прочитать или сохранить локальные файлы. Проверьте доступ к папке runtime и свободное место."},
        })

    @app.get("/")
    async def index():
        return FileResponse(PROJECT_ROOT / "frontend/index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    async def health():
        return {
            "status": "ok", "api_key_configured": bool(settings.api_key),
            "provider": settings.ai_provider, "provider_check": "not_performed_by_health_endpoint",
            "text_model": settings.model_for("text"), "image_model": settings.model_for("image"),
            "note": "Это проверка локального сервера. Доступ к модели проверяется отдельным запросом анализа.",
        }

    @app.post("/analyze_text", response_model=AnalysisResponse)
    async def analyze_text(body: TextAnalysisRequest):
        return await workflows.analyze("text", body.text,
            SourceInfo(name=body.source_name, url=body.source_url, captured_at=body.captured_at),
            competitor_id=body.competitor_id)

    @app.post("/analyze_image", response_model=AnalysisResponse)
    async def analyze_image(
        file: Annotated[UploadFile, File(description="PNG/JPEG/WEBP, до 8 МБ")],
        competitor_id: Annotated[CompetitorId | None, Form()] = None,
        source_name: Annotated[str | None, Form(max_length=200)] = None,
        source_url: Annotated[HttpUrl | None, Form()] = None,
        captured_at: Annotated[AwareDatetime | None, Form()] = None,
    ):
        try:
            data = await file.read(MAX_IMAGE_BYTES + 1)
            data_url = await run_in_threadpool(prepare_image, data)
        finally:
            await file.close()
        return await workflows.analyze("image", data_url,
            SourceInfo(name=source_name or (file.filename or "Изображение")[:200],
                       url=source_url, captured_at=captured_at), competitor_id=competitor_id)

    app.include_router(router(workflows))
    app.include_router(workspace_router(history))
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "frontend"), name="static")

    return app
