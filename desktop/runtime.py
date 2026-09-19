"""Own a loopback server and keep user data outside the executable bundle."""
import shutil
import socket
import sys
import threading
from pathlib import Path

import uvicorn
from backend.config import PROJECT_ROOT, Settings
from backend.main import create_app


def default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "Srez-data"
    return PROJECT_ROOT


def load_settings(data_dir: Path) -> Settings:
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    env_path = data_dir / ".env"
    if not env_path.exists():
        shutil.copyfile(PROJECT_ROOT / ".env.example", env_path)
    # Packaged resources are read-only; persistent files always use the chosen folder.
    return Settings(_env_file=env_path, runtime_dir=data_dir / "runtime")


class RequestTracker:
    def __init__(self, app):
        self.app = app
        self.pending = 0

    async def __call__(self, scope, receive, send):
        tracked = scope["type"] == "http" and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if tracked:
            self.pending += 1
        try:
            await self.app(scope, receive, send)
        finally:
            if tracked:
                self.pending -= 1


class LocalServer:
    def __init__(self, settings):
        self.tracker = RequestTracker(create_app(settings))
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.error = None
        self.server = uvicorn.Server(uvicorn.Config(
            self.tracker, host="127.0.0.1", port=self.port,
            loop="asyncio", http="h11", ws="none", lifespan="on",
            log_config=None, access_log=False, log_level="error",
            timeout_graceful_shutdown=15,
        ))
        self.thread = threading.Thread(target=self._run, name="srez-server", daemon=True)

    def _run(self):
        try:
            self.server.run(sockets=[self.socket])
        except BaseException as exc:
            # Never surface exception payloads which may contain provider settings.
            self.error = type(exc).__name__
        finally:
            self.socket.close()

    @property
    def ready(self):
        return self.server.started and self.thread.is_alive()

    @property
    def busy(self):
        return self.tracker.pending > 0

    def start(self):
        self.thread.start()

    def stop(self, timeout=8):
        self.server.should_exit = True
        if self.thread.ident:
            self.thread.join(timeout)
        else:
            self.socket.close()
        return not self.thread.is_alive()
