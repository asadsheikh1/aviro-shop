"""Settings shared by every AVIRO service (env prefix ``AVIRO_``)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AVIRO_", extra="ignore")

    service: str = "unknown"
    log_level: str = "INFO"

    database_url: str = "postgresql://aviro:aviro-dev-only@localhost:5432/aviro"
    db_pool_min: int = Field(default=1, ge=0)
    db_pool_max: int = Field(default=10, ge=1)
    db_pool_timeout_s: float = 2.0
    db_statement_timeout_ms: int = 2000

    broker_url: str = "redis://localhost:6379/0"

    inventory_url: str = "http://inventory:8000"
    payments_url: str = "http://payments:8000"
    orders_url: str = "http://orders:8000"
    http_timeout_s: float = 3.0

    # Runtime fault flags and stats require this token. Unset = admin API disabled.
    admin_token: SecretStr | None = None
    data_dir: str = "/data"


@lru_cache
def get_settings() -> Settings:
    return Settings()
