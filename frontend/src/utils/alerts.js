import Swal from "sweetalert2";

/**
 * Displays a success alert dialog.
 * @param {string} message - The message to display.
 * @returns {Promise} SweetAlert2 result promise.
 */
export function showSuccessAlert(message) {
  return Swal.fire({
    title: "Success",
    text: message,
    icon: "success",
    heightAuto: false,
  });
}

/**
 * Displays an error alert dialog.
 * @param {string} message - The error message to display.
 * @returns {Promise} SweetAlert2 result promise.
 */
export function showErrorAlert(message) {
  return Swal.fire({
    title: "Error",
    text: message,
    icon: "error",
    heightAuto: false,
  });
}

/**
 * Displays a warning alert dialog.
 * @param {string} message - The warning message to display.
 * @returns {Promise} SweetAlert2 result promise.
 */
export function showWarningAlert(message) {
  return Swal.fire({
    title: "Alert",
    text: message,
    icon: "warning",
    heightAuto: false,
  });
}

/**
 * Prompts the user to confirm replacing the currently loaded picknote.
 * @returns {Promise<{isConfirmed: boolean}>} SweetAlert2 result promise.
 */
export function confirmClear() {
  return Swal.fire({
    title: "Clear picknote?",
    text: "All scan progress will be lost. Do you want to continue?",
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "Clear",
    cancelButtonText: "Cancel",
    heightAuto: false,
  });
}

export function confirmSubmit(picknote) {
  return Swal.fire({
    title: "Submit picknote?",
    text: `Submit ${picknote} to the system?`,
    icon: "question",
    showCancelButton: true,
    confirmButtonText: "Submit",
    cancelButtonText: "Cancel",
    heightAuto: false,
  });
}

export function confirmReplacePicknote() {
  return Swal.fire({
    title: "Replace current picknote?",
    text: "Current picknote will be cleared. Do you want to continue?",
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "OK",
    cancelButtonText: "Cancel",
    heightAuto: false,
  });
}

/**
 * Prompts the user to confirm advancing to the next pack box.
 * @param {number} currentPackbox - The pack box number currently being filled.
 * @returns {Promise<{isConfirmed: boolean}>} SweetAlert2 result promise.
 */
export function confirmNextPackbox(currentPackbox) {
  const next = (Number(currentPackbox) || 1) + 1;
  return Swal.fire({
    title: "Start next pack box?",
    html: `You are about to close <b>Pack Box ${currentPackbox}</b> and start filling <b>Pack Box ${next}</b>.<br/><br/>This action cannot be undone for this picknote.`,
    icon: "warning",
    showCancelButton: true,
    confirmButtonText: "OK",
    cancelButtonText: "Cancel",
    heightAuto: false,
  });
}

/**
 * Displays an interactive dialog listing ambiguous product candidates for the user to select from.
 * @param {Array<{product_name: string, batch_number: string}>} candidates - List of matching product candidates.
 * @returns {Promise<{isConfirmed: boolean, selectedIndex: number|null}>} Whether confirmed and the chosen index.
 */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "—";
  return div.innerHTML;
}

export async function showAmbiguousAlert(candidates, scannedBatch) {
  if (!Array.isArray(candidates) || candidates.length === 0) {
    return { isConfirmed: false, selectedIndex: null };
  }
  let selectedIndex = null;

  const rowsHtml = candidates
    .map(
      (c, i) => `
      <div id="amb-row-${i}" style="display:flex;align-items:center;padding:10px 8px;border-bottom:1px solid #e5e7eb;gap:12px;border-radius:4px;transition:background 0.15s;cursor:default">
        <div style="flex:1;text-align:left">
          <div style="font-weight:600;font-size:14px">${escapeHtml(c.product_name)}</div>
          <div style="font-size:12px;color:#6b7280">${escapeHtml(c.batch_number)}</div>
        </div>
        <span id="amb-btn-${i}" role="button" style="padding:4px 14px;border:1px solid #3b82f6;border-radius:4px;background:#3b82f6;color:white;cursor:pointer;font-size:13px;user-select:none">Select</span>
      </div>`,
    )
    .join("");

  const scannedHtml = scannedBatch
    ? `<div style="margin-bottom:12px;padding:8px 12px;background:#f0fdf4;border:1px solid #86efac;border-radius:6px;font-size:13px;color:#166534">
        <span style="font-weight:600">Scanned:</span> ${escapeHtml(scannedBatch)}
       </div>`
    : "";

  const { isConfirmed } = await Swal.fire({
    title: "Ambiguous Match",
    html: `
      ${scannedHtml}
      <p style="margin-bottom:12px;color:#374151">Multiple products matched with same matching score.<br/> Select the correct one:</p>
      <div style="max-height:320px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:6px">${rowsHtml}</div>
    `,
    showCancelButton: true,
    confirmButtonText: "Confirm",
    cancelButtonText: "Skip",
    heightAuto: false,
    didOpen: () => {
      const confirmBtn = Swal.getConfirmButton();
      if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.style.opacity = "0.5";
        confirmBtn.style.cursor = "not-allowed";
      }

      candidates.forEach((_, i) => {
        document.getElementById(`amb-btn-${i}`)?.addEventListener("click", () => {
          candidates.forEach((_, j) => {
            const row = document.getElementById(`amb-row-${j}`);
            const btn = document.getElementById(`amb-btn-${j}`);
            if (row) row.style.background = "";
            if (btn) {
              btn.style.background = "#3b82f6";
              btn.style.opacity = "1";
              btn.style.cursor = "pointer";
              btn.textContent = "Select";
            }
          });
          const row = document.getElementById(`amb-row-${i}`);
          const btn = document.getElementById(`amb-btn-${i}`);
          if (row) row.style.background = "#eff6ff";
          if (btn) {
            btn.style.background = "#1d4ed8";
            btn.style.opacity = "0.7";
            btn.style.cursor = "default";
            btn.textContent = "Selected";
          }
          selectedIndex = i;
          if (confirmBtn) {
            confirmBtn.disabled = false;
            confirmBtn.style.opacity = "1";
            confirmBtn.style.cursor = "pointer";
          }
        });
      });
    },
  });

  return { isConfirmed, selectedIndex: isConfirmed ? selectedIndex : null };
}
