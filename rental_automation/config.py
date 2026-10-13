"""Environment-backed application configuration."""

from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "sqlite:///./rental_automation.db"
    timezone: str = "Asia/Kolkata"
    payment_match_threshold: float = Field(default=0.82, ge=0.0, le=1.0)
    payment_amount_tolerance: float = Field(default=1.00, ge=0.0)
    notification_backend: str = "mock"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    payment_upi_vpa: str = ""
    payment_upi_payee_name: str = "Property Manager"
    reminder_deadline_day: int = Field(default=5, ge=1, le=31)
    kseb_wss_user_id: str = ""
    kseb_wss_password: str = ""
    kseb_consumer_numbers: str = ""
    kseb_consumer_number: str = ""
    kseb_fetch_day: int = Field(default=15, ge=1, le=28)
    public_app_url: str = ""
    app_auth_username: str = ""
    app_auth_password: str = ""
    id_proof_encryption_key: str = ""

    @model_validator(mode="after")
    def validate_public_agreement_access(self) -> "Settings":
        if self.public_app_url:
            parsed_url = urlsplit(self.public_app_url)
            if (
                parsed_url.scheme != "https"
                or not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError("PUBLIC_APP_URL must use HTTPS for remote tenant and signing links")
            if not self.app_auth_username or not self.app_auth_password:
                raise ValueError(
                    "APP_AUTH_USERNAME and APP_AUTH_PASSWORD are required when PUBLIC_APP_URL is set"
                )
            if not self.id_proof_encryption_key:
                raise ValueError(
                    "ID_PROOF_ENCRYPTION_KEY is required when PUBLIC_APP_URL is set"
                )
            try:
                Fernet(self.id_proof_encryption_key.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError("ID_PROOF_ENCRYPTION_KEY must be a valid Fernet key") from exc
        return self

    @field_validator("notification_backend")
    @classmethod
    def validate_notification_backend(cls, value: str) -> str:
        allowed = {"mock", "twilio_sms", "twilio_whatsapp"}
        if value not in allowed:
            raise ValueError(f"notification_backend must be one of {sorted(allowed)}")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith(("sqlite:///", "postgresql://", "postgresql+")):
            raise ValueError("database_url must use SQLite or PostgreSQL")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
