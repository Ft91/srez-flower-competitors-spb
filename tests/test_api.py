import base64
import io
import json

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from backend.config import Settings
from backend.main import create_app
from backend.service import AnalysisService, MAX_IMAGE_BYTES

TEXT_RESULT = {
    "summary": "По изученному материалу: тестовый ответ, не реальный анализ.",
    "facts": {name: {"status": "not_stated", "value": None, "evidence_quotes": []}
              for name in ["assortment", "origin_countries", "farm_names", "minimum_order",
                           "packaging_and_mixes", "availability_and_preorder", "delivery",
                           "payment", "claims", "ordering", "quality_promises"]},
    "price_examples": [], "claimed_advantages": [], "purchase_constraints": [],
    "questions_to_supplier": ["Каков минимальный заказ?"],
    "limitations": ["Только переданный материал."],
}
IMAGE_RESULT = {
    "description": "Тестовый скриншот.",
    "visible_business_terms": {"status": "not_stated", "value": None, "evidence_quotes": []},
    "design_score": 7, "design_score_reason": "Тестовое объяснение.",
    **{key: {"score": 7, "reason": "Тестовое объяснение."} for key in
       ["readability", "b2b_offer_clarity", "navigation_clarity", "contact_visibility", "ordering_path_clarity"]},
    "recommendations": [], "limitations": ["Виден только один экран, оценка субъективна."],
}


def settings(**kwargs):
    return Settings(_env_file=None, openai_api_key="", proxy_api_key="", **kwargs)


def completion(result, *, finish="stop", refusal=None):
    # Текстовые фикстуры хранят публичный формат; провайдер возвращает только ID.
    def draft(value):
        if isinstance(value, dict):
            return {("evidence_line_ids" if k == "evidence_quotes" else k):
                    (list(range(1, len(v) + 1)) if k == "evidence_quotes" else draft(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [draft(v) for v in value]
        return value
    if isinstance(result, str):
        try:
            body = json.loads(result)
            if isinstance(body, dict) and "facts" in body:
                body = {k: v for k, v in body.items() if k not in {"summary", "questions_to_supplier", "limitations"}}
                result = json.dumps(draft(body))
            elif isinstance(body, dict) and "design_score" in body:
                body.pop("limitations", None)
                result = json.dumps(body)
        except ValueError:
            pass
    return {
        "id": "test-completion", "object": "chat.completion", "created": 1,
        "model": "test-model", "choices": [{
            "index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": result, "refusal": refusal},
        }],
    }


def mock_client(handler, *, provider="openai"):
    cfg = Settings(_env_file=None, ai_provider=provider,
                   openai_api_key="test-openai-key", proxy_api_key="test-proxy-key")
    service = AnalysisService(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return TestClient(create_app(cfg, service))


def png():
    buffer = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private_note", "must-not-be-forwarded")
    Image.new("RGB", (24, 16), "white").save(buffer, "PNG", pnginfo=metadata)
    return buffer.getvalue()


def test_no_key_server_works_but_analysis_reports_setup_needed():
    with TestClient(create_app(settings())) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["api_key_configured"] is False
        assert client.get("/docs").status_code == 200
        response = client.post("/analyze_text", json={"text": "Пример текста компании"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "api_key_missing"
        image = client.post("/analyze_image", files={"file": ("screen.png", png(), "image/png")})
        assert image.status_code == 503


@pytest.mark.parametrize("body", [
    {"text": " " * 20}, {"text": "a" * 40001},
    {"text": "Пример текста компании", "source_url": "file:///secret"},
])
def test_invalid_input_rejected_before_api(body):
    with TestClient(create_app(settings())) as client:
        assert client.post("/analyze_text", json=body).status_code == 422


@pytest.mark.parametrize("provider,host,model,key", [
    ("openai", "api.openai.com", "gpt-4.1-mini", "test-openai-key"),
    ("proxyapi", "api.proxyapi.ru", "openai/gpt-4.1-mini", "test-proxy-key"),
])
def test_text_uses_actual_sdk_with_strict_schema_and_provider_routing(provider, host, model, key):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == host
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer " + key
        payload = json.loads(request.content)
        assert payload["model"] == model
        assert payload["store"] is False
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert json.loads(payload["messages"][1]["content"])["source_lines"] == [{"id": 1, "text": "Исходный текст конкурента"}]
        return httpx.Response(200, json=completion(json.dumps(TEXT_RESULT)))

    with mock_client(handler, provider=provider) as client:
        result = client.post("/analyze_text", json={
            "text": "Исходный текст конкурента", "source_name": "Проверка",
            "source_url": "https://example.com/",
        })
    assert result.status_code == 200, result.text
    assert result.json()["analysis"]["facts"] == TEXT_RESULT["facts"]
    assert result.json()["analysis"]["price_examples"] == []
    assert result.json()["source"]["url"] == "https://example.com/"
    assert len(requests) == 1


def test_image_reaches_sdk_as_valid_png_without_metadata():
    def handler(request):
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        image_url = content[1]["image_url"]
        assert image_url["detail"] == "high"
        raw = base64.b64decode(image_url["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (24, 16)
            assert "private_note" not in image.info
        return httpx.Response(200, json=completion(json.dumps(IMAGE_RESULT)))

    with mock_client(handler) as client:
        result = client.post("/analyze_image", files={"file": ("screen.png", png(), "image/png")})
    assert result.status_code == 200, result.text
    assert result.json()["analysis"]["design_score"] == IMAGE_RESULT["design_score"]
    assert result.json()["analysis"]["description"] == IMAGE_RESULT["description"]
    assert len(result.json()["analysis"]["limitations"]) == 3
    assert result.json()["source"]["name"] == "screen.png"


@pytest.mark.parametrize("data,status", [
    (b"not-an-image", 415), (b"x" * (MAX_IMAGE_BYTES + 1), 413),
], ids=["invalid-image", "oversized-image"])
def test_bad_or_oversized_file_rejected(data, status):
    with TestClient(create_app(settings())) as client:
        assert client.post("/analyze_image", files={"file": ("fake.png", data)}).status_code == status


@pytest.mark.parametrize("status,expected", [(401, 503), (403, 503), (429, 503), (500, 502)])
def test_provider_errors_do_not_leak_body_or_key(status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "test-openai-key PRIVATE ORIGINAL TEXT"}})

    with mock_client(handler) as client:
        response = client.post("/analyze_text", json={"text": "Исходный текст конкурента"})
    assert response.status_code == expected
    assert "PRIVATE" not in response.text
    assert "test-openai-key" not in response.text
    assert len(calls) == 1  # No hidden paid retries.


def test_timeout_is_reported():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    with mock_client(handler) as client:
        response = client.post("/analyze_text", json={"text": "Исходный текст конкурента"})
    assert response.status_code == 504


@pytest.mark.parametrize("result,finish,refusal,status", [
    ("not json", "stop", None, 502),
    ("{}", "stop", None, 502),
    (json.dumps(TEXT_RESULT), "length", None, 502),
    (None, "stop", "Cannot analyze", 422),
])
def test_invalid_incomplete_or_refused_result_never_becomes_success(result, finish, refusal, status):
    def handler(request):
        return httpx.Response(200, json=completion(result, finish=finish, refusal=refusal))

    with mock_client(handler) as client:
        response = client.post("/analyze_text", json={"text": "Исходный текст конкурента"})
    assert response.status_code == status
    assert response.json()["success"] is False


def test_out_of_range_visual_score_is_rejected():
    def handler(request):
        invalid = dict(IMAGE_RESULT, design_score=11)
        return httpx.Response(200, json=completion(json.dumps(invalid)))

    with mock_client(handler) as client:
        response = client.post("/analyze_image", files={"file": ("screen.png", png())})
    assert response.status_code == 502


@pytest.mark.parametrize("nested", [True, False], ids=["nested-error", "flat-error"])
def test_region_denial_is_distinct_from_invalid_key(nested):
    def handler(request):
        error = {"code": "unsupported_country_region_territory", "type": "request_forbidden",
                 "message": "PRIVATE provider response test-openai-key"}
        return httpx.Response(403, json={"error": error} if nested else error)

    with mock_client(handler) as client:
        response = client.post("/analyze_text", json={"text": "Исходный текст конкурента"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_region_unavailable"
    assert "PRIVATE" not in response.text
    assert "test-openai-key" not in response.text

def result_with_fact(quote, status="stated_by_supplier"):
    import copy
    result = copy.deepcopy(TEXT_RESULT)
    result["facts"]["minimum_order"] = {
        "status": status, "value": "От одной коробки.", "evidence_quotes": [quote],
    }
    return result


def test_invented_source_reference_rejects_entire_analysis():
    result = result_with_fact("Не используется: цитаты подставляет сервер.")
    result["facts"]["minimum_order"].pop("evidence_quotes")
    result["facts"]["minimum_order"]["evidence_line_ids"] = [99999]
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": "Поставка цветов в Санкт-Петербург."})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "evidence_mismatch"


def test_quotes_allow_whitespace_normalization_and_keep_source_metadata():
    result = result_with_fact("От одной коробки.")
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={
            "text": "От\u00a0одной\tкоробки.",
            "source_name": "Условия", "source_url": "https://example.com/terms",
            "captured_at": "2026-09-18T10:00:00Z",
        })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "0.4"
    assert body["evidence_check"]["quotes_checked"] == 1
    assert body["analysis"]["facts"]["minimum_order"]["evidence_quotes"] == ["От\u00a0одной\tкоробки."]
    assert body["source"]["url"] == "https://example.com/terms"
    assert body["source"]["captured_at"] == "2026-09-18T10:00:00Z"


@pytest.mark.parametrize("status", ["not_stated", "conflicting"])
def test_inconsistent_fact_status_does_not_pass(status):
    result = result_with_fact("От одной коробки.", status)
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": "Цветы от одной коробки."})
    assert response.status_code == 502


def test_price_keeps_from_unit_and_unknown_date_and_variety():
    import copy
    text = "Роза. 50 см. Цена от 90 руб/шт. Пакинг: 400 шт (1 коробка)."
    result = copy.deepcopy(TEXT_RESULT)
    result["price_examples"] = [{
        "flower": "Роза", "variety": None, "stem_length_cm": 50,
        "stems_per_box": 400, "amount": 90, "currency": "RUB",
        "unit": "stem", "price_kind": "from", "valid_date_label": None,
        "conditions": None, "evidence_quotes": [text],
    }]
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": text, "captured_at": "2026-09-18T10:00:00Z"})
    assert response.status_code == 200, response.text
    price = response.json()["analysis"]["price_examples"][0]
    assert price["amount"] == 90 and price["price_kind"] == "from"
    assert price["unit"] == "stem" and price["stems_per_box"] == 400
    assert price["variety"] is None and price["valid_date_label"] is None
    check = response.json()["price_checks"][0]
    assert set(check["missing_parameters"]) == {"сорт", "дата или период цены"}
    assert "нижняя граница" in check["note"]


def test_valid_conflicting_terms_require_both_real_quotes():
    result = result_with_fact("Минимум 1 коробка.", "conflicting")
    result["facts"]["minimum_order"]["value"] = "В источнике есть два разных минимума."
    result["facts"]["minimum_order"]["evidence_quotes"].append("Минимум 2 коробки.")
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": "Минимум 1 коробка.\nМинимум 2 коробки."})
    assert response.status_code == 200, response.text
    assert response.json()["evidence_check"]["quotes_checked"] == 2


def test_image_source_metadata_and_unassessable_score():
    import copy
    result = copy.deepcopy(IMAGE_RESULT)
    result["ordering_path_clarity"] = {"score": None, "reason": "Часть экрана перекрыта."}
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_image", files={"file": ("screen.png", png(), "image/png")}, data={
            "source_name": "ФлораДомСПБ — первый экран", "source_url": "https://floradomspb.ru/",
            "captured_at": "2026-09-18T10:00:00Z",
        })
    assert response.status_code == 200, response.text
    assert response.json()["analysis"]["ordering_path_clarity"]["score"] is None
    assert response.json()["evidence_check"]["mode"] == "visual_review_required"
    assert response.json()["source"]["url"] == "https://floradomspb.ru/"


def test_three_source_fragments_for_one_finding_are_accepted():
    import copy
    result = copy.deepcopy(TEXT_RESULT)
    result["claimed_advantages"] = [{"text": "По заявлению поставщика: поставки и миксы.",
                                    "evidence_quotes": ["Цветы оптом.", "Микс сортов.", "Доставка по СПб."]}]
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": "Цветы оптом.\nМикс сортов.\nДоставка по СПб."})
    assert response.status_code == 200, response.text
    assert response.json()["analysis"]["claimed_advantages"][0]["evidence_quotes"] == result["claimed_advantages"][0]["evidence_quotes"]


def test_vague_worldwide_supply_does_not_become_country_list_or_foreign_summary():
    import copy
    result = copy.deepcopy(TEXT_RESULT)
    result["summary"] = "Американский ассортимент, которого нет в источнике."
    result["facts"]["origin_countries"] = {
        "status": "stated_by_supplier", "value": "со всего мира",
        "evidence_quotes": ["Цветы со всего мира."],
    }
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": "Цветы со всего мира."})
    assert response.status_code == 200, response.text
    body = response.json()["analysis"]
    assert body["facts"]["origin_countries"]["status"] == "not_stated"
    assert "Американ" not in body["summary"]
    assert "неправильная" not in " ".join(body["limitations"])


def test_supplier_questions_do_not_repeat_known_minimum_or_payment():
    import copy
    result = copy.deepcopy(TEXT_RESULT)
    for key, value in [("minimum_order", "От одной коробки."), ("payment", "100% предоплата.")]:
        result["facts"][key] = {"status": "stated_by_supplier", "value": value, "evidence_quotes": [value]}
    result["questions_to_supplier"] = ["Продаёте ли вы в розницу?", "Какой минимальный заказ?"]
    with mock_client(lambda r: httpx.Response(200, json=completion(json.dumps(result)))) as client:
        response = client.post("/analyze_text", json={"text": "От одной коробки. 100% предоплата."})
    assert response.status_code == 200, response.text
    questions = " ".join(response.json()["analysis"]["questions_to_supplier"])
    assert "розницу" not in questions and "минимальный заказ" not in questions
    assert "предоплаты" not in questions


def test_descriptive_variety_is_flagged_for_review():
    from backend.evidence import price_checks
    from backend.schemas import CompetitorAnalysis
    import copy
    result = copy.deepcopy(TEXT_RESULT)
    result["price_examples"] = [{
        "flower": "Роза Дэвида Остина", "variety": "махровый цветок, имеет тонкий аромат",
        "stem_length_cm": None, "stems_per_box": 288, "amount": 260,
        "currency": "RUB", "unit": "stem", "price_kind": "from",
        "valid_date_label": None, "conditions": None, "evidence_quotes": ["от 260 руб/шт"],
    }]
    check = price_checks(CompetitorAnalysis.model_validate(result))[0]
    assert "название сорта требует проверки" in check.missing_parameters
