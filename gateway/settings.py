"""Gateway configuration, loaded from environment variables (and `.env` in development).

Every setting here is documented in `.env.example`.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # The real Anthropic key. Only the gateway holds it; clients never see it.
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_base_url: str = "https://api.anthropic.com"
    # Upstream timeout in seconds. Long generations can take minutes.
    upstream_timeout_s: float = 600.0

    # SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@localhost:5432/gateway
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"

    # When false, only metrics are stored (no prompts or responses).
    store_content: bool = False

    price_table_path: Path = PROJECT_ROOT / "config" / "prices.yaml"

    # Models the gateway itself uses in later stages (handoff docs, classification).
    handoff_model: str = "claude-sonnet-5-5"
    classifier_model: str = "claude-haiku-5-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
