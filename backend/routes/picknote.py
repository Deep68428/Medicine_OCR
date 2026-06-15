from fastapi import APIRouter, HTTPException
from loguru import logger
import urllib.parse
from core.deps import get_remote_db
from sqlalchemy import text

router = APIRouter(
    prefix="/picknote",
    tags=["picknote"],
)


@router.post("/search")
async def search(data: dict):
    """Retrieve picknote line items by querying the remote DB.

    Fetches product, batch, expiry, and quantity details for all stock move lines tied
    to the given picknote. Numeric and date fields are normalized before returning.

    Args:
        data: Request body containing ``picknote`` (document name) and ``machine_id``.

    Returns:
        dict: Status and ``picknote_data`` list, or an error message if not found.
    """
    picknote = data.get("picknote")
    async with get_remote_db() as remote_db_session:
        result = await remote_db_session.execute(
            text("""
                SELECT
                spl.product_id as product_id,
                pt.name->>'en_US' AS product_name,
                lot.name AS batch_number,
                lot.exp_date as expiry_date,
                lot.mrp as mrp,
                lot.old_mrp as old_mrp,
                SUM(spl.quantity) AS batch_quantity,
                from stock_move_line as spl
                LEFT JOIN stock_picking AS sp ON spl.picking_id = sp.id
                Left join stock_lot as lot on lot.product_id = spl.product_id
                LEFT JOIN product_product AS pp ON pp.id = spl.product_id
                LEFT JOIN product_template AS pt ON pt.id = pp.product_tmpl_id
                WHERE sp.name = :picknote and spl.lot_id = lot.id
                GROUP BY  spl.product_id,pt.name,lot.name,lot.exp_date,lot.mrp,lot.old_mrp;
            """),
            {"picknote": picknote},
        )

        picknote_data = result.mappings().all()
        picknote_data = [dict(row) for row in picknote_data]
        for i, row in enumerate(picknote_data):
            row["row_index"] = i
            row["batch_quantity"] = (
                int(row["batch_quantity"])
                if row.get("batch_quantity") is not None
                else 0
            )
            row["strip_in_box"] = (
                int(row["strip_in_box"]) if row.get("strip_in_box") is not None else 1
            )
            row["mrp"] = float(row["mrp"]) if row.get("mrp") is not None else 0.0
            row["old_mrp"] = (
                float(row["old_mrp"]) if row.get("old_mrp") is not None else 0.0
            )
            row["expiry_date"] = (
                row["expiry_date"].strftime("%Y-%m-%d")
                if row.get("expiry_date")
                else None
            )
            row["done_quantity"] = 0
            row["pending_quantity"] = row["batch_quantity"]
            row["error_quantity"] = 0
            row["batch_corrections"] = []
            row["scan_log"] = []

    if not picknote_data:
        return {"status": "error", "message": "Picknote data not found"}

    return {"status": "success", "picknote_data": picknote_data}


@router.get("/batches")
async def get_product_batches(picknote: str):
    """Return all stock lots for every product that appears in the given picknote.

    Used by the machine controller to resolve batch-correction scans — when an
    OCR-read batch is not in the picklist but belongs to a product that is.

    Args:
        picknote: The picknote document name (e.g. ``"PICK001"``).

    Returns:
        dict: Status and a ``batches`` list, each entry with product_name,
            product_code, batch_number, mrp, mfg_date, exp_date.
    """
    if not picknote:
        raise HTTPException(status_code=400, detail="picknote is required")
    # decode url encoded picknote to handle special characters
    picknote = urllib.parse.unquote(picknote)
    async with get_remote_db() as remote_db_session:
        result = await remote_db_session.execute(
            text("""
                SELECT DISTINCT
                    pt.name AS product_name,
                    pt.default_code AS product_code,
                    lot.name AS batch_number,
                    lot.mrp,
                    lot.mfg_date,
                    lot.exp_date
                FROM stock_picking sp
                JOIN stock_move_line sml ON sml.picking_id = sp.id
                JOIN product_product pp ON pp.id = sml.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                JOIN stock_production_lot lot ON lot.product_id = pp.id
                WHERE sp.name = :picknote
                ORDER BY pt.name, lot.name
            """),
            {"picknote": picknote},
        )
        batches = [dict(row._mapping) for row in result.fetchall()]

    return {"status": "success", "batches": batches}


@router.post("/submit")
async def submit(data: dict):
    picknote = data.get("picknote")
    products = data.get("products", [])
    if not picknote:
        raise HTTPException(status_code=400, detail="picknote is required")
    try:
        async with get_remote_db() as remote_db_session:
            logger.info(
                f"[REQUEST START] picknote={picknote!r} "
                f"total_products={len(products)}"
            )
            # Fetch all existing stock_move_line rows for this picking
            fetch_existing_query = text("""
                SELECT
                    spl.id                  AS line_id,
                    spl.product_id          AS product_id,
                    sp.id                   AS picking_id,
                    sp.name,
                    sp.start_qc_date,
                    sp.end_qc_date,
                    pt.name                 AS product_name,
                    pt.default_code         AS product_code,
                    pt.uom_id               AS product_uom_id,
                    COALESCE(pt.second_size, '0') AS pack_size,
                    lot.name                AS batch,
                    lot.id                  AS lot_id,
                    spl.product_uom_qty,
                    spl.location_id,
                    (spl.product_uom_qty - spl.qty_done) AS pending_qty
                FROM stock_move_line AS spl
                LEFT JOIN stock_picking AS sp
                    ON spl.picking_id = sp.id
                LEFT JOIN product_product AS pp
                    ON pp.id = spl.product_id
                LEFT JOIN product_template AS pt
                    ON pt.id = pp.product_tmpl_id
                LEFT JOIN stock_production_lot AS lot
                    ON lot.id = spl.lot_id AND lot.product_id = pp.id
                WHERE sp.name = :picknote
                AND (spl.product_uom_qty - spl.qty_done) > 0
            """)
            logger.info(
                f"""
                [FETCH EXISTING QUERY]
                Query:
                {fetch_existing_query.text}

                Params:
                {{"picknote": {picknote!r}}}
                """
            )
            result = await remote_db_session.execute(
                fetch_existing_query, {"picknote": picknote}
            )
            existing_lines = [dict(row) for row in result.mappings().all()]

            logger.info(
                f"[FETCH EXISTING RESULT] "
                f"picknote={picknote!r} "
                f"rows_found={len(existing_lines)}"
            )

            # Index by (product_name, batch)
            lines_by_batch: dict = {}

            for line in existing_lines:
                key = (line["product_name"], line["batch"])
                lines_by_batch[key] = line

            # Fetch reference rows
            fetch_reference_query = text("""
                SELECT DISTINCT ON (pt.name)
                    spl.picking_id,
                    spl.product_id,
                    pt.name         AS product_name,
                    pt.uom_id       AS product_uom_id,
                    spl.location_id
                FROM stock_move_line AS spl
                LEFT JOIN stock_picking AS sp ON spl.picking_id = sp.id
                LEFT JOIN product_product AS pp ON pp.id = spl.product_id
                LEFT JOIN product_template AS pt ON pt.id = pp.product_tmpl_id
                WHERE sp.name = :picknote
            """)

            logger.info(
                f"""
            [FETCH REFERENCE QUERY]
            Query:
            {fetch_reference_query.text}

            Params:
            {{"picknote": {picknote!r}}}
            """
            )
            ref_result = await remote_db_session.execute(
                fetch_reference_query, {"picknote": picknote}
            )
            lines_by_product: dict = {}
            ref_rows = ref_result.mappings().all()

            for row in ref_rows:
                lines_by_product[row["product_name"]] = dict(row)

            logger.info(
                f"[FETCH REFERENCE RESULT] "
                f"picknote={picknote!r} "
                f"reference_rows={len(ref_rows)}"
            )
            updates = 0
            inserts_pending = 0
            for index, product in enumerate(products, start=1):
                product_name = product.get("product_name", "")
                batch_number = product.get("batch_number", "")
                qty_done = product.get("qty_done", 0)
                box_number = product.get("box_number", "")
                scaning_status = product.get("scaning_status", "OCR")
                logger.info(
                    f"""
                    [PROCESS PRODUCT]
                    index={index}
                    product_name={product_name!r}
                    batch_number={batch_number!r}
                    qty_done={qty_done}
                    box_number={box_number!r}
                    scaning_status={scaning_status!r}
                    """
                )
                existing = lines_by_batch.get((product_name, batch_number))
                if existing:
                    update_query = text("""
                        UPDATE stock_move_line
                        SET qty_done        = :qty_done,
                            box_number      = :box_number,
                            scaning_status  = :scaning_status
                        WHERE id = :line_id
                    """)
                    update_params = {
                        "qty_done": qty_done,
                        "box_number": box_number,
                        "scaning_status": scaning_status,
                        "line_id": existing["line_id"],
                    }
                    logger.info(
                        f"""
                    [UPDATE QUERY]
                    Query:
                    {update_query.text}

                    Params:
                    {update_params}
                    """
                    )
                    await remote_db_session.execute(update_query, update_params)
                    updates += 1
                    logger.info(
                        f"[UPDATE SUCCESS] "
                        f"picknote={picknote!r} "
                        f"product={product_name!r} "
                        f"batch={batch_number!r} "
                        f"line_id={existing['line_id']} "
                        f"qty_done={qty_done} "
                        f"box_number={box_number!r}"
                    )
                else:
                    ref = lines_by_product.get(product_name)
                    if ref:
                        insert_query = text("""
                            INSERT INTO stock_move_line
                                (picking_id, product_id, qty_done, lot_id, company_id,
                                 product_uom_id, date, product_uom_qty, location_id,
                                 location_dest_id, box_number, scaning_status)
                            VALUES (
                                :picking_id,
                                :product_id,
                                :qty_done,
                                (SELECT id FROM stock_production_lot
                                 WHERE name = :batch_number AND product_id = :product_id
                                 LIMIT 1),
                                1,
                                :product_uom_id,
                                NOW()::timestamp,
                                :qty_done,
                                COALESCE(
                                   (SELECT
                                    st.location_id
                                    FROM stock_quant AS st
                                    LEFT JOIN stock_location AS sl ON sl.id = st.location_id
                                    LEFT JOIN stock_production_lot AS lot ON lot.id = st.lot_id
                                    LEFT JOIN product_product AS pp ON pp.id = st.product_id
                                    LEFT JOIN product_template AS pt ON pt.id = pp.product_tmpl_id
                                    INNER JOIN stock_move_line AS sml ON sml.lot_id = st.lot_id AND sml.product_id = st.product_id
                                    INNER JOIN stock_picking AS sp ON sp.id = sml.picking_id
                                    WHERE sl.usage = 'internal'
                                      AND st.product_id = :product_id
                                      AND lot.name = :batch_number
                                      AND st.quantity > 0
                                      AND sl.m_branch_id = (
                                                           SELECT m_branch_id FROM stock_picking
                                                           WHERE id = :picking_id LIMIT 1
                                                       )
                                      AND sl.is_temp_location is null
                                    ORDER BY sl.is_bin_location
                                    LIMIT 1),
                                    :fallback_location_id
                                ),
                                5,
                                :box_number,
                                :scaning_status
                            )
                        """)
                        insert_params = {
                            "picking_id": ref["picking_id"],
                            "product_id": ref["product_id"],
                            "qty_done": qty_done,
                            "batch_number": batch_number,
                            "product_uom_id": ref["product_uom_id"],
                            "fallback_location_id": ref["location_id"],
                            "box_number": box_number,
                            "scaning_status": scaning_status,
                        }
                        logger.info(
                            f"""
                            [INSERT QUERY]
                            Query:
                            {insert_query.text}

                            Params:
                            {insert_params}
                            """
                        )
                        select_subquery = text("""
                                               SELECT
                                    st.location_id
                                    FROM stock_quant AS st
                                    LEFT JOIN stock_location AS sl ON sl.id = st.location_id
                                    LEFT JOIN stock_production_lot AS lot ON lot.id = st.lot_id
                                    LEFT JOIN product_product AS pp ON pp.id = st.product_id
                                    LEFT JOIN product_template AS pt ON pt.id = pp.product_tmpl_id
                                    INNER JOIN stock_move_line AS sml ON sml.lot_id = st.lot_id AND sml.product_id = st.product_id
                                    INNER JOIN stock_picking AS sp ON sp.id = sml.picking_id
                                    WHERE sl.usage = 'internal'
                                      AND st.product_id = :product_id
                                      AND lot.name = :batch_number
                                      AND st.quantity > 0
                                      AND sl.m_branch_id = (
                                                           SELECT m_branch_id FROM stock_picking
                                                           WHERE id = :picking_id LIMIT 1
                                                       )
                                      AND sl.is_temp_location is null
                                    ORDER BY sl.is_bin_location
                                    LIMIT 1;""")
                        # log the output of the select subquery for debugging
                        subquery_result = await remote_db_session.execute(
                            select_subquery,
                            {
                                "product_id": ref["product_id"],
                                "batch_number": batch_number,
                                "picking_id": ref["picking_id"],
                            },
                        )
                        subquery_location = subquery_result.scalar()
                        # log the output of the subquery
                        logger.info(
                            f"""
                            [SUBQUERY RESULT]
                            product_id={ref['product_id']}
                            batch_number={batch_number!r}
                            picking_id={ref['picking_id']}
                            location_id={subquery_location}
                            """
                        )
                        await remote_db_session.execute(insert_query, insert_params)
                        inserts_pending += 1
                        logger.info(
                            f"[INSERT SUCCESS] "
                            f"picknote={picknote!r} "
                            f"product={product_name!r} "
                            f"batch={batch_number!r} "
                            f"qty_done={qty_done} "
                            f"box_number={box_number!r} "
                            f"(picking_id={ref['picking_id']}, "
                            f"product_id={ref['product_id']})"
                        )
                    else:
                        logger.warning(
                            f"[INSERT FAILED] "
                            f"picknote={picknote!r} "
                            f"product={product_name!r} "
                            f"batch={batch_number!r} "
                            f"qty_done={qty_done} "
                            f"box_number={box_number!r} "
                            f"(no reference line found)"
                        )
            logger.info(
                f"[COMMIT START] "
                f"picknote={picknote!r} "
                f"updates={updates} "
                f"inserts_pending={inserts_pending}"
            )
            await remote_db_session.commit()
            logger.success(
                f"[REQUEST SUCCESS] "
                f"picknote={picknote!r} "
                f"updates={updates} "
                f"inserts_pending={inserts_pending}"
            )
        return {
            "status": "success",
            "updates": updates,
            "inserts_pending": inserts_pending,
        }

    except Exception as e:
        logger.exception(
            f"[REQUEST FAILED] " f"picknote={picknote!r} " f"error={str(e)}"
        )
        raise HTTPException(status_code=500, detail=str(e))
