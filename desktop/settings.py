"""Atomic updates to the selected project's .env, without touching its history."""
from pathlib import Path
import os
import tempfile
from dotenv import set_key
from backend.config import Settings


def save_connection(data_dir: Path, values: dict):
    allowed = {"AI_PROVIDER", "OPENAI_API_KEY", "PROXY_API_KEY", "TEXT_MODEL", "IMAGE_MODEL"}
    if set(values) != allowed or any(any(c in v for c in "\r\n\0") for v in values.values()):
        raise ValueError("Invalid connection settings")
    Settings(_env_file=None, **{k.lower(): v for k, v in values.items()})
    env = data_dir / ".env"
    fd, name = tempfile.mkstemp(prefix=".env-", dir=data_dir)
    os.close(fd)
    temp = Path(name)
    try:
        temp.write_bytes(env.read_bytes() if env.exists() else b"")
        for key, value in values.items():
            set_key(str(temp), key, value.strip(), quote_mode="always")
        os.replace(temp, env)
    finally:
        temp.unlink(missing_ok=True)
