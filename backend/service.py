import base64
import io
import json
import warnings
from typing import Literal

import httpx2 as httpx
from openai import (
    APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI,
    ContentFilterFinishReasonError, LengthFinishReasonError,
)
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import ValidationError

from .config import Settings
from .schemas import CompetitorAnalysis, ImageAnalysis
from .prompts import TEXT_PROMPT, IMAGE_PROMPT
from .evidence import EvidenceMismatch, verify_text_evidence, finalize_text_analysis
from .grounding import CompetitorAnalysisDraft, ImageAnalysisDraft, hydrate_evidence, source_lines

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000


class AnalysisError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def prepare_image(data: bytes) -> str:
    if len(data) > MAX_IMAGE_BYTES:
        raise AnalysisError(413, "image_too_large", "Размер изображения должен быть не более 8 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                if img.format not in {"PNG", "JPEG", "WEBP"}:
                    raise AnalysisError(415, "unsupported_image", "Поддерживаются PNG, JPEG и WEBP.")
                if img.width * img.height > MAX_IMAGE_PIXELS:
                    raise AnalysisError(413, "too_many_pixels", "Изображение слишком большое: максимум 25 млн пикселей. Пришлите отдельный фрагмент.")
                if getattr(img, "n_frames", 1) > 1:
                    raise AnalysisError(415, "animated_image", "Пришлите статичное изображение.")
                img.verify()
            # Декодирование обнаруживает повреждённые файлы; пересохранение убирает метаданные.
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                clean = ImageOps.exif_transpose(img).convert("RGB")
                output = io.BytesIO()
                output_format = "JPEG" if img.format == "JPEG" else "PNG"
                clean.save(output, format=output_format)
        if output.tell() > MAX_IMAGE_BYTES:
            raise AnalysisError(413, "normalized_image_too_large", "После обработки изображение превышает 8 МБ. Пришлите меньший фрагмент.")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        mime = "image/jpeg" if output_format == "JPEG" else "image/png"
        return "data:" + mime + ";base64," + encoded
    except AnalysisError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AnalysisError(413, "too_many_pixels", "Изображение слишком большое.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise AnalysisError(415, "invalid_image", "Не удалось прочитать изображение. Проверьте файл.") from None


class AnalysisService:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = None
        if settings.api_key:
            self.client = AsyncOpenAI(
                api_key=settings.api_key, base_url=settings.base_url,
                timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10),
                max_retries=0, http_client=http_client,
            )

    async def close(self):
        if self.client:
            await self.client.close()

    async def analyze_text(self, text: str):
        lines = source_lines(text)
        content = json.dumps({"source_lines": lines}, ensure_ascii=False)
        draft = await self._analyze("text", content, CompetitorAnalysisDraft)
        try:
            result = finalize_text_analysis(hydrate_evidence(draft, lines))
            evidence_check = verify_text_evidence(result, text)
        except EvidenceMismatch:
            raise AnalysisError(502, "evidence_mismatch", "Модель сослалась на отсутствующий фрагмент источника. Анализ не принят; проверьте материал и повторите запрос.") from None
        except ValidationError:
            raise AnalysisError(502, "invalid_response", "Ответ модели противоречит правилам анализа: проверьте исходный материал и повторите запрос.") from None
        return result, evidence_check

    async def analyze_image(self, data_url: str):
        content = [
            {"type": "text", "text": "Проанализируй видимый экран для покупателя оптовых цветов в Санкт-Петербурге. Невидимое или нечитаемое оставляй неизвестным."},
            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
        ]
        draft = await self._analyze("image", content, ImageAnalysisDraft)
        return ImageAnalysis.model_validate({
            **draft.model_dump(),
            "limitations": [
                "Оценён только переданный статичный экран; скрытые разделы и мобильная версия не проверялись.",
                "По изображению нельзя проверить кликабельность, работу форм, скорость сайта и качество цветов.",
                "Оценки субъективны; распознавание текста и интерпретацию деталей изображения нужно проверить.",
            ],
        })

    async def _analyze(self, kind: Literal["text", "image"], content, schema):
        if not self.client:
            raise AnalysisError(503, "api_key_missing", "API-ключ не настроен. Заполните .env и перезапустите приложение.")
        try:
            completion = await self.client.chat.completions.parse(
                model=self.settings.model_for(kind),
                messages=[{"role": "system", "content": TEXT_PROMPT if kind == "text" else IMAGE_PROMPT}, {"role": "user", "content": content}],
                response_format=schema,
                max_completion_tokens=self.settings.max_output_tokens,
                store=False,
            )
            if not completion.choices:
                raise AnalysisError(502, "empty_response", "Модель вернула пустой ответ.")
            choice = completion.choices[0]
            if choice.message.refusal:
                raise AnalysisError(422, "model_refusal", "Модель отказалась анализировать материал. Попробуйте другой фрагмент.")
            if choice.finish_reason != "stop" or choice.message.parsed is None:
                raise AnalysisError(502, "invalid_response", "Модель не вернула полный ответ нужного формата.")
            return choice.message.parsed
        except APITimeoutError:
            raise AnalysisError(504, "provider_timeout", "Сервис ИИ не ответил вовремя. Попробуйте позже.") from None
        except APIConnectionError:
            raise AnalysisError(502, "provider_unreachable", "Нет соединения с сервисом ИИ.") from None
        except APIStatusError as exc:
            # Тело ошибки провайдера может содержать данные запроса: не выдаём его клиенту.
            body = exc.body if isinstance(exc.body, dict) else {}
            details = body.get("error", body)
            code = details.get("code") if isinstance(details, dict) else None
            if exc.status_code == 403 and code == "unsupported_country_region_territory":
                raise AnalysisError(503, "provider_region_unavailable", "Сервис ИИ отклонил запрос из-за региона подключения. Проверьте провайдера и условия доступа для места запуска.") from None
            if exc.status_code in {401, 403}:
                raise AnalysisError(503, "provider_access_denied", "Проверьте API-ключ и доступ к выбранной модели.") from None
            if exc.status_code == 429:
                raise AnalysisError(503, "provider_limit", "Проверьте баланс и лимиты сервиса ИИ.") from None
            raise AnalysisError(502, "provider_error", "Сервис ИИ отклонил запрос. Проверьте модель и настройки подключения.") from None
        except ContentFilterFinishReasonError:
            raise AnalysisError(422, "model_refusal", "Сервис ИИ не смог обработать этот материал.") from None
        except LengthFinishReasonError:
            raise AnalysisError(502, "incomplete_response", "Ответ модели оборвался. Сократите исходный текст или увеличьте лимит ответа.") from None
        except (ValidationError, ValueError):
            raise AnalysisError(502, "invalid_response", "Ответ модели не соответствует формату анализа.") from None
        except APIError:
            raise AnalysisError(502, "provider_error", "Ошибка сервиса ИИ. Попробуйте позже.") from None
