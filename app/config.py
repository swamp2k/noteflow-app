from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    secret_key: str = Field(default="dev-secret-key-change-me")
    base_url: str = Field(default="http://localhost:8000")
    database_url: str = Field(default="sqlite+aiosqlite:///./noteflow.db")
    upload_dir: str = Field(default="./uploads")
    anthropic_api_key: str = Field(default="")
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    session_expire_seconds: int = Field(default=604800)


settings = Settings()
