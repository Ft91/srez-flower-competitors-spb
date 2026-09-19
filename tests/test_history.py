import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.config import Settings
from backend.history import HistoryStore
from backend.main import create_app
from backend.parsing_service import BROWSER_LOCK, ParsingService
from backend.schemas import CompetitorAnalysis, EvidenceCheck, ImageAnalysis
from backend.service import AnalysisError
from test_api import TEXT_RESULT, IMAGE_RESULT


def settings(**kw):
    return Settings(_env_file=None, openai_api_key="", proxy_api_key="", **kw)


class FakeParser:
    def __init__(self, text="Цветы оптом. Минимальный заказ одна коробка.", fail=None, usable=True):
        self.text, self.fail, self.usable = text, fail, usable
        self.calls = []

    def collect(self, competitor_id):
        self.calls.append(competitor_id)
        if competitor_id == self.fail:
            raise AnalysisError(504, "page_timeout", "Сайт не ответил.")
        data = io.BytesIO()
        Image.new("RGB", (100, 80), "white").save(data, "PNG")
        meta = {"url": "https://floradomspb.ru/", "captured_at": "2026-09-19T00:00:00+00:00",
                "text_characters": len(self.text), "usable_for_analysis": self.usable}
        return meta, {"page.txt": (self.text.encode(), "text/plain"),
                      "viewport.png": (data.getvalue(), "image/png")}


class FakeAnalyzer:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def close(self):
        pass

    async def analyze_text(self, text):
        self.calls.append(("text", text))
        if self.fail:
            raise AnalysisError(503, "test_failure", "Проверочная ошибка.")
        return CompetitorAnalysis.model_validate(TEXT_RESULT), EvidenceCheck(
            mode="text_quotes_matched", quotes_checked=0, note="Тест")

    async def analyze_image(self, content):
        self.calls.append(("image", content))
        return ImageAnalysis.model_validate(IMAGE_RESULT)


def test_collect_persists_restart_and_repeat_adds_record(tmp_path):
    cfg = settings(runtime_dir=tmp_path)
    with TestClient(create_app(cfg, FakeAnalyzer(), FakeParser())) as client:
        first = client.post("/collect", json={"competitor_ids": ["floradomspb"]}).json()["items"][0]
        second = client.post("/collect", json={"competitor_ids": ["floradomspb"]}).json()["items"][0]
        assert first["id"] != second["id"]
        assert client.get(first["artifacts"]["page.txt"]["url"]).text.startswith("Цветы оптом")
    with TestClient(create_app(cfg, FakeAnalyzer(), FakeParser())) as client:
        records = client.get("/history").json()
        assert records["total"] == 2
        assert records["items"][0]["id"] == second["id"]
        assert client.get("/history/" + first["id"]).json()["result"]["usable_for_analysis"]
        assert client.get(first["artifacts"]["page.txt"]["url"]).status_code == 200


def test_demo_creates_capture_and_two_related_analyses():
    analyzer = FakeAnalyzer()
    with TestClient(create_app(settings(), analyzer, FakeParser())) as client:
        response = client.post("/parsedemo", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["success"]
        capture = body["items"][0]["capture"]
        assert len(body["items"][0]["analyses"]) == 2
        records = client.get("/history?competitor_id=floradomspb").json()["items"]
        assert len(records) == 3
        assert all(r["parent_id"] == capture["id"] for r in records if r["kind"] != "capture")
        assert client.get("/history?kind=image").json()["total"] == 1
        for item in body["items"][0]["analyses"]:
            result = item["result"]
            saved = client.get("/history/" + result["history_id"]).json()
            assert saved["result"] == result
    assert [kind for kind, _ in analyzer.calls] == ["text", "image"]


def test_failed_capture_does_not_stop_rest_and_no_ai_calls():
    analyzer = FakeAnalyzer()
    with TestClient(create_app(settings(), analyzer, FakeParser(fail="optflor"))) as client:
        result = client.post("/collect", json={"competitor_ids": ["optflor", "floradomspb"]}).json()
        assert result["success"] is False
        assert [r["status"] for r in result["items"]] == ["failed", "completed"]
        assert result["items"][0]["error"]["code"] == "page_timeout"
        assert client.get("/history").json()["total"] == 2
        assert analyzer.calls == []


def test_failed_analysis_is_recorded_and_image_continues():
    with TestClient(create_app(settings(), FakeAnalyzer(fail=True), FakeParser())) as client:
        result = client.post("/parsedemo", json={}).json()
        assert result["success"] is False
        assert [r["success"] for r in result["items"][0]["analyses"]] == [False, True]
        records = client.get("/history?kind=text").json()["items"]
        assert len(records) == 1 and records[0]["status"] == "failed"
        assert records[0]["error"]["code"] == "test_failure"
        assert client.get(records[0]["artifacts"]["input.txt"]["url"]).status_code == 200


def test_blocked_capture_kept_but_not_analyzed():
    analyzer = FakeAnalyzer()
    with TestClient(create_app(settings(), analyzer, FakeParser(usable=False))) as client:
        result = client.post("/parsedemo", json={}).json()
        assert result["success"] is False
        assert result["items"][0]["error"]["code"] == "capture_not_usable"
        assert client.get("/history").json()["total"] == 1
    assert not analyzer.calls


def test_long_page_is_preserved_and_analyzed_input_is_explicitly_limited():
    analyzer = FakeAnalyzer()
    original = "Цветы. " * 6000
    with TestClient(create_app(settings(), analyzer, FakeParser(text=original))) as client:
        result = client.post("/parsedemo", json={"modes": ["text"]}).json()
        item = result["items"][0]
        analysis = item["analyses"][0]["result"]
        assert "40 000" in analysis["input_notes"][0]
        assert client.get(item["capture"]["artifacts"]["page.txt"]["url"]).text == original
        assert len(analyzer.calls[0][1]) <= 40000


def test_duplicates_are_not_repeated():
    parser, analyzer = FakeParser(), FakeAnalyzer()
    with TestClient(create_app(settings(), analyzer, parser)) as client:
        result = client.post("/parsedemo", json={"competitor_ids": ["floradomspb"] * 2, "modes": ["text", "text"]}).json()
        assert result["success"]
    assert len(parser.calls) == 1
    assert len(analyzer.calls) == 1


@pytest.mark.parametrize("body", [
    {"competitor_ids": ["http://127.0.0.1"]}, {"competitor_ids": []},
    {"competitor_ids": ["floradomspb"], "url": "file:///secret"},
    {"competitor_ids": ["floradomspb"] * 6},
])
def test_only_configured_competitors_allowed(body):
    parser = FakeParser()
    with TestClient(create_app(settings(), FakeAnalyzer(), parser)) as client:
        assert client.post("/collect", json=body).status_code == 422
    assert parser.calls == []


def test_history_pagination_and_artifact_path_protection():
    with TestClient(create_app(settings(), FakeAnalyzer(), FakeParser())) as client:
        result = client.post("/collect", json={"competitor_ids": ["floradomspb", "optflor"]}).json()
        page = client.get("/history?limit=1&offset=1").json()
        assert page["total"] == 2 and len(page["items"]) == 1
        assert page["items"][0]["id"] == result["items"][0]["id"]
        record_id = result["items"][0]["id"]
        assert client.get(f"/history/{record_id}/files/.env").status_code == 404
        assert client.get("/history/not-a-uuid").status_code == 422
        assert client.get("/history/" + "0" * 32).status_code == 404
        assert client.get("/history?limit=1000").status_code == 422
        assert client.get("/history?offset=-1").status_code == 422


def test_manual_text_and_image_are_also_in_history():
    parser, analyzer = FakeParser(), FakeAnalyzer()
    with TestClient(create_app(settings(), analyzer, parser)) as client:
        body = client.post("/analyze_text", json={"text": "Заказ от одной коробки"}).json()
        assert client.get("/history/" + body["history_id"]).json()["result"] == body
        assert client.get("/history").json()["total"] == 1


def test_concurrent_history_writes_do_not_overwrite(tmp_path):
    store = HistoryStore(tmp_path)
    def write(number):
        record = store.new("text")
        return store.finish(record, result={"number": number})["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(24)))
    assert len(set(ids)) == 24
    assert store.list(limit=100)["total"] == 24
    assert {store.get(i)["result"]["number"] for i in ids} == set(range(24))


def test_browser_busy_is_controlled():
    BROWSER_LOCK.acquire()
    try:
        with pytest.raises(AnalysisError) as error:
            ParsingService(settings()).collect("optflor")
        assert error.value.code == "browser_busy"
    finally:
        BROWSER_LOCK.release()


def test_driver_quits_and_lock_releases_on_navigation_failure():
    from selenium.common.exceptions import TimeoutException
    class Driver:
        quit_called = False
        def set_page_load_timeout(self, value): pass
        def set_script_timeout(self, value): pass
        def execute_cdp_cmd(self, *args): pass
        def get(self, url): raise TimeoutException("do not expose this raw detail")
        def quit(self): self.quit_called = True
    driver = Driver()
    with pytest.raises(AnalysisError) as error:
        ParsingService(settings(), lambda profile: driver).collect("optflor")
    assert error.value.code == "page_timeout"
    assert "raw detail" not in error.value.message
    assert driver.quit_called and not BROWSER_LOCK.locked()

def test_changed_capture_is_not_sent_to_ai(tmp_path):
    analyzer = FakeAnalyzer()
    cfg = settings(runtime_dir=tmp_path)
    with TestClient(create_app(cfg, analyzer, FakeParser())) as client:
        capture = client.post("/collect", json={"competitor_ids": ["floradomspb"]}).json()["items"][0]
        path = tmp_path / "history" / "artifacts" / capture["id"] / "page.txt"
        path.write_text("Изменённый материал", encoding="utf-8")
        response = client.post(f"/captures/{capture['id']}/analyze", json={"modes": ["text"]})
        assert response.json()["items"][0]["error"]["code"] == "artifact_changed"
        assert not analyzer.calls


@pytest.mark.parametrize("blocked,height", [(False, 900), (False, 18000), (True, 900)])
def test_parser_capture_metadata_limits_and_cleanup(blocked, height):
    import base64
    data = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(data, "PNG")
    class Driver:
        current_url = "https://www.optflor.com/"
        title = "Just a moment" if blocked else "Оптовые цветы"
        capabilities = {"browserVersion": "test"}
        quit_called = False
        clips = []
        def set_page_load_timeout(self, value): pass
        def set_script_timeout(self, value): pass
        def get(self, url): self.requested = url
        def find_element(self, *args): return True
        def execute_async_script(self, script): return True
        def get_screenshot_as_png(self): return data.getvalue()
        def execute_script(self, script):
            return "Цветы оптом от одной коробки." if "innerText" in script else height
        def execute_cdp_cmd(self, command, args):
            if command == "Page.captureScreenshot":
                self.clips.append(args["clip"])
                return {"data": base64.b64encode(data.getvalue()).decode()}
        def quit(self): self.quit_called = True
    driver = Driver()
    cfg = settings()
    result, files = ParsingService(cfg, lambda profile: driver).collect("optflor")
    assert result["usable_for_analysis"] is not blocked
    assert result["full_page"]["height"] == min(height, 12000)
    assert driver.clips[0]["height"] == min(height, 12000)
    assert set(files) == {"page.txt", "page.json", "viewport.png", "full-page.png"}
    assert driver.quit_called and not BROWSER_LOCK.locked()
    assert not list((cfg.runtime_dir / "browser-profiles").iterdir())


def test_manual_image_history_and_file_are_readable():
    from test_api import png
    with TestClient(create_app(settings(), FakeAnalyzer(), FakeParser())) as client:
        response = client.post("/analyze_image", files={"file": ("image.png", png(), "image/png")})
        assert response.status_code == 200
        record = client.get("/history/" + response.json()["history_id"]).json()
        image = client.get(record["artifacts"]["input.png"]["url"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(image.content)) as img:
            assert "private_note" not in img.info
