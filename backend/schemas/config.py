from typing import Optional
from pydantic import BaseModel, field_validator


class MachineConfig(BaseModel):
    machine_id: int
    camera_serial: str
    conveyor_ip: str
    conveyor_port: int
    start_conveyor: str
    accept_conveyor: str
    reject_conveyor: str
    exposure_time_us: Optional[float] = None
    # Remote config delivery — populated server-side, consumed by machine_code
    gitlab_url: Optional[str] = None
    gitlab_project_id: Optional[int] = None
    gitlab_token: Optional[str] = None
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    loki_url: Optional[str] = None
    minio_endpoint: Optional[str] = None
    minio_access_key: Optional[str] = None
    minio_secret_key: Optional[str] = None
    minio_bucket: Optional[str] = None
    minio_secure: Optional[bool] = None

    @field_validator("machine_id")
    @classmethod
    def machine_id_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("machine_id must be a positive integer")
        return v

    @field_validator("conveyor_port")
    @classmethod
    def conveyor_port_must_be_valid(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("conveyor_port must be between 1 and 65535")
        return v

    @field_validator("camera_serial")
    @classmethod
    def camera_serial_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("camera_serial must not be empty")
        return v.strip()

    @field_validator("conveyor_ip")
    @classmethod
    def conveyor_ip_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conveyor_ip must not be empty")
        return v.strip()

    @field_validator("start_conveyor", "accept_conveyor", "reject_conveyor")
    @classmethod
    def conveyor_commands_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conveyor command must not be empty")
        return v

    @field_validator("gitlab_project_id")
    @classmethod
    def gitlab_project_id_must_be_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("gitlab_project_id must be a positive integer")
        return v
