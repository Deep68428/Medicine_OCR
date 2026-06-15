from __future__ import annotations

import json
from typing import TYPE_CHECKING

import aiosqlite
from loguru import logger

from core.config import get_config

if TYPE_CHECKING:
    from services.state import MachineState


async def _get_db_path() -> str:
    return get_config().STATE_DB_PATH


async def _ensure_table(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS machine_state (
            machine_id           INTEGER PRIMARY KEY,
            picknote             TEXT,
            products             TEXT,
            current_product      TEXT,
            current_packbox      INTEGER DEFAULT 1,
            product_batch_list   TEXT
        )
        """
    )
    for migration in (
        "ALTER TABLE machine_state ADD COLUMN current_packbox INTEGER DEFAULT 1",
        "ALTER TABLE machine_state ADD COLUMN product_batch_list TEXT",
    ):
        try:
            await db.execute(migration)
        except Exception:
            pass
    await db.commit()


async def save_state(state: MachineState) -> None:
    """Persist the scan-related fields of MachineState to SQLite.

    Uses INSERT OR REPLACE so it is always a single-row upsert per machine_id.
    Errors are logged but never raised — persistence must not break the scan flow.

    Args:
        state: The current MachineState instance.
    """
    try:
        db_path = await _get_db_path()
        async with aiosqlite.connect(db_path) as db:
            await _ensure_table(db)
            await db.execute(
                """
                INSERT OR REPLACE INTO machine_state
                    (machine_id, picknote, products, current_product, current_packbox, product_batch_list)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state.machine_id,
                    state.picknote,
                    json.dumps(state.products),
                    json.dumps(state.current_product)
                    if state.current_product is not None
                    else None,
                    int(state.current_packbox or 1),
                    json.dumps(state.product_batch_list),
                ),
            )
            await db.commit()
        logger.debug(
            f"State persisted for machine_id={state.machine_id} picknote={state.picknote!r}"
        )
    except Exception as exc:
        logger.error(f"Failed to persist state: {exc}")


async def load_and_restore(state: MachineState) -> None:
    """Load the last persisted state for this machine_id and restore it into state.

    Restores ``picknote``, ``products``, and ``current_product``.
    If no row is found or an error occurs, state is left unchanged.

    Args:
        state: The MachineState instance to restore into.
    """
    try:
        db_path = await _get_db_path()
        async with aiosqlite.connect(db_path) as db:
            await _ensure_table(db)
            async with db.execute(
                "SELECT picknote, products, current_product, current_packbox, product_batch_list FROM machine_state WHERE machine_id = ?",
                (state.machine_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:
            logger.info(f"No persisted state found for machine_id={state.machine_id}")
            return

        (
            picknote,
            products_json,
            current_product_json,
            current_packbox,
            product_batch_list_json,
        ) = row
        state.picknote = picknote
        state.products = json.loads(products_json) if products_json else []
        state.current_product = (
            json.loads(current_product_json) if current_product_json else None
        )
        state.current_packbox = (
            int(current_packbox) if current_packbox is not None else 1
        )
        state.product_batch_list = (
            json.loads(product_batch_list_json) if product_batch_list_json else []
        )
        logger.info(
            f"Restored state for machine_id={state.machine_id} picknote={picknote!r} "
            f"products_count={len(state.products)} "
            f"product_batch_list_count={len(state.product_batch_list)}"
        )
    except Exception as exc:
        logger.error(f"Failed to restore state: {exc}")
