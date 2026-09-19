from fastapi.testclient import TestClient
from backend.main import create_app
from backend.history import HistoryStore
from test_history import settings, FakeAnalyzer, FakeParser
from test_api import png


def test_ui_and_assets_served_without_exposing_private_files():
    with TestClient(create_app(settings(), FakeAnalyzer(), FakeParser())) as client:
        page=client.get("/")
        assert page.status_code==200 and "text/html" in page.headers["content-type"]
        assert 'lang="ru"' in page.text and "/static/app.js" in page.text
        assert client.get("/static/app.js").status_code==200
        assert client.get("/static/app.css").status_code==200
        for path in ["/.env","/static/.env","/static/../.env"]:
            assert client.get(path).status_code==404


def test_workspace_empty_does_not_invent_analysis():
    with TestClient(create_app(settings(), FakeAnalyzer(), FakeParser())) as client:
        data=client.get("/workspace").json()
        assert data["example"] is False and len(data["items"])==5
        assert data["summary"]=={"competitors":5,"captures":0,"analyses":0}
        assert all(i["text"] is None and i["image"] is None for i in data["items"])


def test_examples_are_marked_and_do_not_write_to_history():
    analyzer=FakeAnalyzer()
    with TestClient(create_app(settings(), analyzer, FakeParser())) as client:
        before=client.get("/history").json()["total"]
        data=client.get("/examples/workspace").json()
        assert data["example"] is True and len(data["records"])==3
        assert data["summary"]["analyses"]==3
        assert all(r["example"] for r in data["records"])
        assert sum(i["text"] is not None for i in data["items"])==2
        assert sum(i["image"] is not None for i in data["items"])==1
        assert next(i for i in data["items"] if i["id"]=="optflor")["notes"]
        assert client.get("/history").json()["total"]==before
        assert not analyzer.calls


def test_manual_materials_link_to_supplier_and_latest_success_wins():
    analyzer=FakeAnalyzer()
    with TestClient(create_app(settings(),analyzer,FakeParser())) as client:
        first=client.post("/analyze_text",json={"text":"Первый материал компании","competitor_id":"optflor"}).json()
        latest=client.post("/analyze_text",json={"text":"Новый материал компании","competitor_id":"optflor"}).json()
        analyzer.fail=True
        assert client.post("/analyze_text",json={"text":"Неудачный новый запрос","competitor_id":"optflor"}).status_code==503
        image=client.post("/analyze_image",files={"file":("test.png",png(),"image/png")},data={"competitor_id":"optflor"}).json()
        data=client.get("/workspace").json()
        item=next(i for i in data["items"] if i["id"]=="optflor")
        assert item["text"]["id"]==latest["history_id"]!=first["history_id"]
        assert item["image"]["id"]==image["history_id"]
        assert item["text"]["competitor_id"]=="optflor"
        assert data["summary"]["analyses"]==3


def test_invalid_supplier_rejected_before_ai():
    analyzer=FakeAnalyzer()
    with TestClient(create_app(settings(),analyzer,FakeParser())) as client:
        assert client.post("/analyze_text",json={"text":"Новый материал компании","competitor_id":"unknown"}).status_code==422
        assert client.post("/analyze_image",files={"file":("test.png",png(),"image/png")},data={"competitor_id":"unknown"}).status_code==422
        assert not analyzer.calls


def test_workspace_capture_and_analysis_are_independent():
    with TestClient(create_app(settings(),FakeAnalyzer(),FakeParser())) as client:
        capture=client.post("/collect",json={"competitor_ids":["floradomspb"]}).json()["items"][0]
        data=client.get("/workspace").json()
        item=next(i for i in data["items"] if i["id"]=="floradomspb")
        assert item["capture"]["id"]==capture["id"]
        assert item["text"] is None and item["image"] is None
        assert data["summary"]["captures"]==1
