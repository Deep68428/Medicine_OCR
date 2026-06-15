from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from core.config import get_db_config
from loguru import logger
from urllib.parse import quote_plus

db_config = get_db_config()

# ---------------------------------------------------
# DATABASE URL MAPPING
# ---------------------------------------------------
REMOTE_DB_USER = quote_plus(db_config.REMOTE_DB_USER)
REMOTE_DB_PASSWORD = quote_plus(db_config.REMOTE_DB_PASSWORD)
REMOTE_DB_HOST = quote_plus(db_config.REMOTE_DB_HOST)
REMOTE_DB_PORT = str(db_config.REMOTE_DB_PORT)
REMOTE_DB_NAME = db_config.REMOTE_DB_NAME

LOCAL_DB_USER = quote_plus(db_config.LOCAL_DB_USER)
LOCAL_DB_PASSWORD = quote_plus(db_config.LOCAL_DB_PASSWORD)
LOCAL_DB_HOST = quote_plus(db_config.LOCAL_DB_HOST)
LOCAL_DB_PORT = str(db_config.LOCAL_DB_PORT)
LOCAL_DB_NAME = db_config.LOCAL_DB_NAME

REMOTE_DATABASE_URLS = {
    "postgres": f"postgresql+asyncpg://{REMOTE_DB_USER}:{REMOTE_DB_PASSWORD}@{REMOTE_DB_HOST}:{REMOTE_DB_PORT}/{REMOTE_DB_NAME}",
    "mysql": f"mysql+aiomysql://{REMOTE_DB_USER}:{REMOTE_DB_PASSWORD}@{REMOTE_DB_HOST}:{REMOTE_DB_PORT}/{REMOTE_DB_NAME}",
    "mssql": f"mssql+aioodbc://{REMOTE_DB_USER}:{REMOTE_DB_PASSWORD}@{REMOTE_DB_HOST}:{REMOTE_DB_PORT}/{REMOTE_DB_NAME}?driver=ODBC+Driver+17+for+SQL+Server",
}

if db_config.REMOTE_DB_TYPE not in REMOTE_DATABASE_URLS:
    raise ValueError(
        f"Unsupported DB_TYPE '{db_config.REMOTE_DB_TYPE}'. "
        f"Supported: {list(REMOTE_DATABASE_URLS.keys())}"
    )

REMOTE_DATABASE_URL = REMOTE_DATABASE_URLS[db_config.REMOTE_DB_TYPE]
LOCAL_DATABASE_URL = (
    f"postgresql+asyncpg://{LOCAL_DB_USER}:{LOCAL_DB_PASSWORD}"
    f"@{LOCAL_DB_HOST}:{LOCAL_DB_PORT}/{LOCAL_DB_NAME}"
)

logger.info(
    f"Remote DB: {db_config.REMOTE_DB_TYPE}://{db_config.REMOTE_DB_HOST}/{REMOTE_DB_NAME}"
)
logger.info(f"Local DB: postgresql://{db_config.LOCAL_DB_HOST}/{LOCAL_DB_NAME}")

# ---------------------------------------------------
# ENGINE
# ---------------------------------------------------
remote_engine = create_async_engine(
    REMOTE_DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
)

local_engine = create_async_engine(
    LOCAL_DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    connect_args={"prepared_statement_cache_size": 0},
)

# ---------------------------------------------------
# SESSION FACTORY
# ---------------------------------------------------
RemoteSessionLocal = async_sessionmaker(
    bind=remote_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

LocalSessionLocal = async_sessionmaker(
    bind=local_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---------------------------------------------------
# DATABASE DEPENDENCY
# ---------------------------------------------------


@asynccontextmanager
async def get_remote_db() -> AsyncIterator[AsyncSession]:
    """Async context manager that yields a SQLAlchemy session bound to the remote database.

    Yields:
        AsyncSession: An active async database session for the remote DB.
    """
    async with RemoteSessionLocal() as session:
        yield session


@asynccontextmanager
async def get_local_db() -> AsyncIterator[AsyncSession]:
    """Async context manager that yields a SQLAlchemy session bound to the local PostgreSQL database.

    Yields:
        AsyncSession: An active async database session for the local DB.
    """
    async with LocalSessionLocal() as session:
        yield session


# ---------------------------------------------------
# SHUTDOWN
# ---------------------------------------------------


async def close_remote_db() -> None:
    """Dispose of all connections in the remote database engine pool."""
    await remote_engine.dispose()


async def close_local_db() -> None:
    """Dispose of all connections in the local database engine pool."""
    await local_engine.dispose()
