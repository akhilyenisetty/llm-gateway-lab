"""Gateway configuration, loaded from environment variables (and `.env` in development).

Every setting here is documented in `.env.example`.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr
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

    # Budgets and alerts. A session's spend budget is this default unless set per session
    # (X-Session-Budget-Usd header, or the admin API, which takes precedence).
    default_session_budget_usd: Annotated[Decimal, Field(gt=0)] = Decimal("5.00")
    # Fractions of the spend budget / context window at which an alert is recorded.
    alert_thresholds: list[Annotated[Decimal, Field(gt=0)]] = [Decimal("0.8"), Decimal("1.0")]

    # Whether clients may set their own session budgets with X-Session-Budget-Usd. When
    # false the header is ignored and only the admin API sets per-session budgets.
    allow_client_budgets: bool = True

    # Bearer token for the /admin API. Empty disables the admin API.
    admin_api_key: SecretStr = SecretStr("")

    # Models the gateway itself uses in later stages (handoff docs, classification).
    handoff_model: str = "claude-sonnet-5-5"
    classifier_model: str = "claude-haiku-5-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
