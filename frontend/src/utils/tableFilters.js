import { TABLE_FILTER } from "../constants";

/**
 * Filters picknote table rows based on the active filter selection.
 * @param {Array<Object>} rows - The full list of product rows.
 * @param {string} activeFilter - The currently active filter (e.g. TABLE_FILTER.PENDING).
 * @returns {Array<Object>} The filtered subset of rows.
 */
export function filterTableRows(rows, activeFilter) {
  return rows.filter((row) => {
    if (activeFilter === TABLE_FILTER.PENDING) {
      return (row.pending_quantity ?? 0) > 0;
    }

    if (activeFilter === TABLE_FILTER.DONE) {
      return (row.done_quantity ?? 0) > 0;
    }

    if (activeFilter === TABLE_FILTER.COMPLETED) {
      return (row.pending_quantity ?? 0) === 0 && (row.done_quantity ?? 0) > 0;
    }

    return true;
  });
}
