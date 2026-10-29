from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_CORS_ORIGINS = ["http://localhost:5173", "http://localhost:3000"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str = Field(min_length=1)
    supabase_secret_key: str = Field(min_length=1, repr=False)
    environment: str = "development"

    # Browser origins allowed to call the API, comma-separated (e.g. "https://shop.example.com").
    # Empty means "the local dev servers" in development, and is an error in production.
    cors_origins: str = ""

    # Payments require a confirmed email. Turn off ONLY while Supabase's "Confirm email" setting is
    # off too (no email service yet); turn back on when emails are added.
    require_confirmed_email: bool = True

    # "dummy": in-process fake, no keys and no network (local dev / tests).
    # "test" / "live": the real Razorpay API with rzp_test_ / rzp_live_ keys.
    razorpay_mode: Literal["dummy", "test", "live"] = "dummy"
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = Field(default=None, repr=False)
    razorpay_webhook_secret: str | None = Field(default=None, repr=False)

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        return origins or ([] if self.is_production else DEV_CORS_ORIGINS)

    @model_validator(mode="after")
    def check_cors(self) -> Self:
        if self.is_production:
            origins = self.cors_origin_list
            if not origins:
                raise ValueError("CORS_ORIGINS must list the frontend origin(s) when ENVIRONMENT=production")
            if "*" in origins:
                raise ValueError("CORS_ORIGINS must not contain * when ENVIRONMENT=production")
        return self

    @model_validator(mode="after")
    def check_razorpay(self) -> Self:
        if self.razorpay_mode == "dummy":
            if self.is_production:
                raise ValueError("RAZORPAY_MODE=dummy is not allowed when ENVIRONMENT=production")
            return self
        missing = [
            name
            for name in ("razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(f"RAZORPAY_MODE={self.razorpay_mode} requires: {', '.join(missing).upper()}")
        prefix = f"rzp_{self.razorpay_mode}_"
        if not self.razorpay_key_id.startswith(prefix):
            # Stops a live key being used in test mode (or the reverse) by accident.
            raise ValueError(f"RAZORPAY_KEY_ID must start with {prefix} when RAZORPAY_MODE={self.razorpay_mode}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
