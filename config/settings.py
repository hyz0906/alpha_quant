from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DEEPSEEK_API_KEY: Optional[str] = None
    TUSHARE_TOKEN: Optional[str] = None
    # Switched to SQLite as default since Docker is skipped
    DATABASE_URL: str = "sqlite:///./alphaquant.db"
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
