from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    database_url: str
    admin_email: str
    admin_password: str

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_hours: int = 12
    refresh_token_expire_days: int = 30

    trust_proxy: bool = False
    dev_mode: bool = False

    display_timezone: str = "Asia/Karachi"

    random_check_window_start: str = "10:00"
    random_check_window_end: str = "17:00"
    random_checks_per_day: int = 4
    check_response_window_minutes: int = 15

    max_devices_per_user: int = 2

    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
