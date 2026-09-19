import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from backend.config import Settings
from desktop.runtime import LocalServer, RequestTracker, default_data_dir, load_settings
from desktop.settings import save_connection
from desktop.qa import self_test


def test_frozen_data_is_next_to_exe_not_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Srez.exe"))
    assert default_data_dir() == tmp_path / "Srez-data"


def test_existing_key_and_history_are_not_replaced(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=local-test-key\n", encoding="utf-8")
    settings = load_settings(tmp_path)
    assert settings.api_key == "local-test-key"
    assert settings.runtime_dir == tmp_path / "runtime"
    assert env.read_text() == "OPENAI_API_KEY=local-test-key\n"


def test_settings_save_preserves_other_parameters_and_quotes(tmp_path):
    (tmp_path / ".env").write_text("CHROME_BINARY='C:/Program Files/Chrome.exe'\nPAGE_TIMEOUT_SECONDS=40\n", encoding="utf-8")
    values = {"AI_PROVIDER":"openai", "OPENAI_API_KEY":"test'key", "PROXY_API_KEY":"",
              "TEXT_MODEL":"custom/model", "IMAGE_MODEL":""}
    save_connection(tmp_path, values)
    saved = dotenv_values(tmp_path / ".env")
    assert all(saved[k] == v for k, v in values.items())
    assert saved["PAGE_TIMEOUT_SECONDS"] == "40"
    assert saved["CHROME_BINARY"] == "C:/Program Files/Chrome.exe"
    assert not list(tmp_path.glob(".env-*"))


def test_settings_invalid_input_does_not_replace_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=original\n", encoding="utf-8")
    values = {"AI_PROVIDER":"openai", "OPENAI_API_KEY":"bad\nkey", "PROXY_API_KEY":"",
              "TEXT_MODEL":"", "IMAGE_MODEL":""}
    with pytest.raises(ValueError):
        save_connection(tmp_path, values)
    assert env.read_text() == "OPENAI_API_KEY=original\n"


def test_tracker_releases_busy_even_after_failure():
    async def run():
        async def failing(scope, receive, send):
            assert tracker.pending == 1
            raise RuntimeError()
        tracker = RequestTracker(failing)
        with pytest.raises(RuntimeError):
            await tracker({"type":"http", "method":"POST"}, None, None)
        assert tracker.pending == 0
    asyncio.run(run())


def test_offline_desktop_lifecycle_and_persistence(tmp_path):
    pytest.importorskip("PyQt6.QtWebEngineWidgets", reason="Install desktop/requirements.txt for desktop diagnostics")
    report = tmp_path / "result.json"
    assert self_test(report) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["ok"]
    assert result["external_requests"] == 0
    assert all(item["ok"] for item in result["checks"])
