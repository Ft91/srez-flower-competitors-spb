"""Offline diagnostics for both source and frozen builds; use isolated data only."""
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from backend.config import PROJECT_ROOT, Settings
from backend.history import HistoryStore
from .runtime import LocalServer


def wait_ready(server, timeout=20):
    end = time.monotonic() + timeout
    while not server.ready and time.monotonic() < end and server.thread.is_alive():
        time.sleep(0.05)
    if not server.ready:
        raise RuntimeError("server_start_failed")


def self_test(report: Path):
    report.parent.mkdir(parents=True, exist_ok=True)
    checks = []
    servers = []
    result = {"test": "desktop_offline", "frozen": bool(getattr(sys, "frozen", False)),
              "external_requests": 0, "checks": checks, "ok": False}
    def check(name, condition):
        checks.append({"name": name, "ok": bool(condition)})
        if not condition:
            raise AssertionError(name)
    opener = build_opener(ProxyHandler({}))
    try:
        from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        result["qt"] = QT_VERSION_STR
        result["pyqt"] = PYQT_VERSION_STR
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        binary = SeleniumManager._get_binary()
        completed = subprocess.run([str(binary), "--version"], capture_output=True, timeout=15,
                                   creationflags=0x08000000 if sys.platform == "win32" else 0)
        check("selenium_manager_packaged_and_executable", completed.returncode == 0)
        check("frontend_resources", (PROJECT_ROOT / "frontend/app.js").is_file())
        check("blank_configuration_template", (PROJECT_ROOT / ".env.example").is_file())
        if getattr(sys, "frozen", False):
            check("private_env_not_bundled", not (PROJECT_ROOT / ".env").exists())
            check("user_history_not_bundled", not (PROJECT_ROOT / "runtime").exists())
        with tempfile.TemporaryDirectory(prefix="srez-check-", dir=report.parent) as temporary:
            data_dir = Path(temporary)
            settings = Settings(_env_file=None, runtime_dir=data_dir / "runtime",
                                ai_provider="openai", openai_api_key="", proxy_api_key="")
            server = LocalServer(settings)
            servers.append(server)
            server.start()
            wait_ready(server)
            def get(path):
                with opener.open(server.url + path, timeout=10) as response:
                    return response.read()
            check("health_without_key", json.loads(get("/health"))["api_key_configured"] is False)
            check("html", b"/static/app.js" in get("/"))
            check("css", len(get("/static/app.css")) > 100)
            check("javascript", len(get("/static/app.js")) > 100)
            before = json.loads(get("/history"))["total"]
            example = json.loads(get("/examples/workspace"))
            check("three_saved_examples", example["example"] and len(example["records"]) == 3)
            check("examples_do_not_change_history", json.loads(get("/history"))["total"] == before == 0)
            check("five_suppliers", len(json.loads(get("/workspace"))["items"]) == 5)
            store = HistoryStore(settings.runtime_dir / "history")
            record = store.new("capture", competitor_id="floradomspb")
            store.put_file(record, "page.txt", "Тест сохранения".encode(), "text/plain")
            store.finish(record, result={"qa": True})
            port = server.port
            check("server_stops", server.stop())
            with socket.socket() as probe:
                check("port_released", probe.connect_ex(("127.0.0.1", port)) != 0)
            server = LocalServer(settings)
            servers.append(server)
            server.start()
            wait_ready(server)
            saved = json.loads(get("/history/" + record["id"]))
            check("history_survives_restart", saved["id"] == record["id"] and saved["status"] == "completed")
            check("artifact_survives_restart", get(f"/history/{record['id']}/files/page.txt").decode() == "Тест сохранения")
            check("restarted_server_stops", server.stop())
        result["ok"] = True
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        for server in servers:
            server.stop()
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
