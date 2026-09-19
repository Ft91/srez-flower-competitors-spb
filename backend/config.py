from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CompetitorId = Literal["7flowers", "optflor", "floradomspb", "florografia", "tsvetomania"]

# Only these public pages can be collected through the local API.
COMPETITORS = {
    "7flowers": {"name": "7ЦВЕТОВ", "url": "https://www.7flowers.ru/?geo=spb"},
    "optflor": {"name": "OptFlor", "url": "https://www.optflor.com/"},
    "floradomspb": {"name": "ФлораДомСПБ", "url": "https://floradomspb.ru/"},
    "florografia": {"name": "Флорография", "url": "https://florografia.ru/"},
    "tsvetomania": {"name": "Цветомания опт", "url": "https://opt.tsvetomania.ru/"},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True,
    )
    runtime_dir: Path = PROJECT_ROOT / "runtime"
    chrome_binary: str = ""
    chrome_driver: str = ""
    page_timeout_seconds: int = Field(default=35, ge=5, le=90)
    ai_provider: Literal["openai", "proxyapi"] = "openai"
    openai_api_key: SecretStr = SecretStr("")
    proxy_api_key: SecretStr = SecretStr("")
    text_model: str = ""
    image_model: str = ""
    request_timeout_seconds: float = Field(default=60, ge=5, le=180)
    max_output_tokens: int = Field(default=6000, ge=256, le=8000)

    @property
    def api_key(self) -> str:
        key = self.openai_api_key if self.ai_provider == "openai" else self.proxy_api_key
        return key.get_secret_value().strip()

    @property
    def base_url(self) -> str:
        return "https://api.openai.com/v1" if self.ai_provider == "openai" else "https://api.proxyapi.ru/v1"

    def model_for(self, kind: Literal["text", "image"]) -> str:
        value = self.text_model if kind == "text" else self.image_model
        default = "gpt-4.1-mini" if self.ai_provider == "openai" else "openai/gpt-4.1-mini"
        return value.strip() or default
