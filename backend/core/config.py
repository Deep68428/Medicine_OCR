from functools import lru_cache
import os

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = os.path.join(os.getcwd())
ENV_PATH = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_PATH)

model_config = SettingsConfigDict(
    env_file=str(ENV_PATH),
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class DBConfig(BaseSettings):
    """Pydantic settings model for local PostgreSQL and remote database configuration."""

    model_config = model_config

    # Local DB Config
    LOCAL_DB_HOST: str = Field(default="localhost")
    LOCAL_DB_PORT: int = Field(default=5432)
    LOCAL_DB_USER: str = Field(default="postgres")
    LOCAL_DB_PASSWORD: str = Field(default="postgres")
    LOCAL_DB_NAME: str = Field(default="ocr_db")

    # Remote DB Config
    REMOTE_DB_HOST: str = Field()
    REMOTE_DB_PORT: int = Field()
    REMOTE_DB_USER: str = Field()
    REMOTE_DB_PASSWORD: str = Field()
    REMOTE_DB_NAME: str = Field()
    REMOTE_DB_TYPE: str = Field()


@lru_cache
def get_db_config() -> DBConfig:
    """Return a cached singleton instance of DBConfig.

    Returns:
        DBConfig: The application database configuration.
    """
    return DBConfig()
