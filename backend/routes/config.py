from fastapi import APIRouter, Body, HTTPException
from fastapi.params import Query
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.config import get_db_config as _get_config
from core.deps import get_local_db
from schemas.config import MachineConfig

router = APIRouter(
    prefix="/config",
    tags=["config"],
)


@router.get("/machine-config")
async def read_machine_config(
    machine_id: int = Query(
        ..., description="The ID of the machine to get the config for"
    ),
):
    """Fetch the configuration record for a given machine from the local database."""
    async with get_local_db() as db:
        result = await db.execute(
            text("SELECT * FROM machine_config WHERE machine_id = :machine_id"),
            {"machine_id": machine_id},
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Machine config not found")
        logger.info(f"Machine config found for machine {machine_id}")
        return MachineConfig.model_validate(dict(row)).model_dump()


@router.post("/machine-config")
async def create_machine_config(
    machine_config: MachineConfig = Body(...),
):
    """Insert a new machine configuration record into the local database."""
    async with get_local_db() as db:
        try:
            await db.execute(
                text(
                    "INSERT INTO machine_config "
                    "(machine_id, camera_serial, exposure_time_us, conveyor_ip, conveyor_port, "
                    "start_conveyor, accept_conveyor, reject_conveyor, "
                    "gitlab_url, gitlab_project_id, gitlab_token, "
                    "model_name, model_version, loki_url, "
                    "minio_endpoint, minio_access_key, minio_secret_key, minio_bucket, minio_secure) "
                    "VALUES (:machine_id, :camera_serial, :exposure_time_us, :conveyor_ip, :conveyor_port, "
                    ":start_conveyor, :accept_conveyor, :reject_conveyor, :gitlab_url, "
                    ":gitlab_project_id, :gitlab_token, :model_name, :model_version, :loki_url, "
                    ":minio_endpoint, :minio_access_key, :minio_secret_key, :minio_bucket, :minio_secure)"
                ),
                machine_config.model_dump(),
            )
            await db.commit()
        except IntegrityError:
            raise HTTPException(
                status_code=409,
                detail=f"Machine config for machine_id={machine_config.machine_id} already exists",
            )
        logger.info(f"Machine config created for machine {machine_config.machine_id}")
    return {"message": "Machine config created successfully"}


@router.put("/machine-config")
async def update_machine_config(
    machine_config: MachineConfig = Body(...),
):
    """Update an existing machine configuration record in the local database."""
    async with get_local_db() as db:
        result = await db.execute(
            text(
                "UPDATE machine_config SET "
                "camera_serial = :camera_serial, exposure_time_us = :exposure_time_us, "
                "conveyor_ip = :conveyor_ip, conveyor_port = :conveyor_port, "
                "start_conveyor = :start_conveyor, "
                "accept_conveyor = :accept_conveyor, reject_conveyor = :reject_conveyor, "
                "gitlab_url = :gitlab_url, gitlab_project_id = :gitlab_project_id, "
                "gitlab_token = :gitlab_token, model_name = :model_name, "
                "model_version = :model_version, loki_url = :loki_url, "
                "minio_endpoint = :minio_endpoint, minio_access_key = :minio_access_key, "
                "minio_secret_key = :minio_secret_key, minio_bucket = :minio_bucket, "
                "minio_secure = :minio_secure "
                "WHERE machine_id = :machine_id"
            ),
            machine_config.model_dump(),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Machine config not found")
        await db.commit()
        logger.info(f"Machine config updated for machine {machine_config.machine_id}")
    return {"message": "Machine config updated successfully"}


@router.delete("/machine-config")
async def delete_machine_config(
    machine_id: int = Query(...),
):
    """Delete the configuration record for a given machine from the local database."""
    async with get_local_db() as db:
        result = await db.execute(
            text("DELETE FROM machine_config WHERE machine_id = :machine_id"),
            {"machine_id": machine_id},
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Machine config not found")
        await db.commit()
        logger.info(f"Machine config deleted for machine {machine_id}")
    return {"message": "Machine config deleted successfully"}


@router.get("/remote-db-config")
async def get_remote_db_config():
    """Return the current remote database connection settings, omitting the password."""
    config = _get_config()
    return {
        "REMOTE_DB_HOST": config.REMOTE_DB_HOST,
        "REMOTE_DB_PORT": config.REMOTE_DB_PORT,
        "REMOTE_DB_USER": config.REMOTE_DB_USER,
        "REMOTE_DB_NAME": config.REMOTE_DB_NAME,
        "REMOTE_DB_TYPE": config.REMOTE_DB_TYPE,
    }
