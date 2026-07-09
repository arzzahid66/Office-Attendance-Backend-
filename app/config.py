from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
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

    # MUST be true in production: uvicorn sits behind a reverse proxy, so without this
    # get_client_ip() returns the proxy's IP for every user and all IP matching is meaningless.
    trust_proxy: bool = False
    dev_mode: bool = False

    # Accepts DISPLAY_TIMEZONE (already set on the VM) or DISPLAY_TZ.
    display_timezone: str = Field(
        "Asia/Karachi", validation_alias=AliasChoices("DISPLAY_TIMEZONE", "DISPLAY_TZ")
    )

    # --- Presence / GPS ---
    # Browser reports coords.accuracy in metres; anything worse than this is rejected.
    max_gps_accuracy_m: int = 100
    # Fallback radius for a new office; each office stores its own radius_meters.
    default_gps_radius_m: int = 80
    # On a GPS day, re-verify the user's location at most this often (battery / permission churn).
    gps_reverify_minutes: int = 15

    # --- Check-out ---
    # The 15-min job closes anyone whose last_seen is older than this.
    checkout_idle_minutes: int = 60

    # --- Scheduler ---
    # Only ONE process may set this. API workers must leave it false; a dedicated
    # scheduler process sets it true. See deploy/attendance-scheduler.service.
    run_scheduler: bool = False

    max_devices_per_user: int = 2

    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
