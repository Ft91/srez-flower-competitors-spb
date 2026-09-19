import pytest
from backend.config import Settings


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    # No automated test writes into the user's real history.
    original = Settings.__init__
    def init(self, **kwargs):
        kwargs.setdefault("runtime_dir", tmp_path / "runtime")
        original(self, **kwargs)
    monkeypatch.setattr(Settings, "__init__", init)
