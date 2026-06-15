"""Configuration for the machine controller."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MachineConfig(BaseSettings):
    """Minimal static config — everything else is fetched from the backend API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Entry point — hardcoded DNS; override in .env only if the server address changes.
    BACKEND_API_URL: str = "http://216.48.178.44:8050/"

    # The only field that must differ per machine.
    MACHINE_ID: int = 1

    # Set DEV_MODE=true in .env to disable Loki logging entirely during development.
    DEV_MODE: bool = False

    # Local paths — not sent over the network.
    STATE_DB_PATH: str = "machine_state.db"
    DEBUG_IMAGE_PATH: str = "~/.config/medicinestrip-ai/Dataset"

    # MinIO image storage — populated from backend machine config at startup.
    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "medicinestrip-ai"
    MINIO_SECURE: bool = False


@lru_cache(maxsize=1)
def get_config() -> MachineConfig:
    """Return the singleton MachineConfig instance, creating it on first call."""
    return MachineConfig()
