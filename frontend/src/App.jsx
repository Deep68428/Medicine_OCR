import { useCallback, useEffect, useRef, useState } from "react";

import "./App.css";
import { ErrorRow, InfoCard, InfoRow } from "./components/InfoCard";
import { StatBox } from "./components/StatBox";
import { STATUS, TABLE_FILTER, TABLE_HEADERS } from "./constants";
import { useMachineSocket } from "./hooks/useMachineSocket";
import {
  confirmClear,
  confirmNextPackbox,
  confirmReplacePicknote,
  confirmSubmit,
  showAmbiguousAlert,
  showErrorAlert,
  showSuccessAlert,
  showWarningAlert,
} from "./utils/alerts";
import { filterTableRows } from "./utils/tableFilters";
import { logError, logInfo } from "./utils/logger";

const MIN_IMAGE_ZOOM = 0.5;
const MAX_IMAGE_ZOOM = 3;
const IMAGE_ZOOM_STEP = 0.25;

export default function PicknoteScanner() {

  const [picknote, setPicknote] = useState("");
  const [machineId, setMachineId] = useState(1);
  const [machineConfig, setMachineConfig] = useState(null);
  const [configStatus, setConfigStatus] = useState("idle");
  const [configError, setConfigError] = useState(null);
  const [isConfigPopupOpen, setIsConfigPopupOpen] = useState(false);
  const [isImageModalOpen, setIsImageModalOpen] = useState(false);
  const [imageZoom, setImageZoom] = useState(1);
  const [partyName, setPartyName] = useState(null);
  const [storeCode, setStoreCode] = useState(null);
  const [stats, setStats] = useState({ total: 0, pending: 0, done: 0, completed: 0 });
  const [tableRows, setTableRows] = useState([]);
  const [doneDrafts, setDoneDrafts] = useState({}); // { [row_index]: string } while a Done Qty cell is being edited
  const [editingDoneIdx, setEditingDoneIdx] = useState(null); // row_index of the Done Qty cell in edit mode
  const doneEditCancelledRef = useRef(false); // set by Escape so the blur-commit reverts instead
  const [activeFilter, setActiveFilter] = useState(TABLE_FILTER.ALL);
  const [cameraImage, setCameraImage] = useState(null);
  const [currentProduct, setCurrentProduct] = useState(null);
  const [scanStatus, setScanStatus] = useState("Idle");
  const [errors, setErrors] = useState({
    detection: null,
    ocr: null,
    matching: null,
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [currentPackbox, setCurrentPackbox] = useState(1);
  const [scanFlash, setScanFlash] = useState(null); // "accepted" | "rejected" | null
  const scanFlashTimerRef = useRef(null);

  const [updateInfo, setUpdateInfo] = useState(null); // { status, version, percent, message }
  const [appVersion, setAppVersion] = useState(null);

  const inputRef = useRef(null);
  const tableRowsRef = useRef(tableRows);
  const pendingSearchRef = useRef(null);
  const imageStageRef = useRef(null);
  const imageZoomRef = useRef(imageZoom);
  const submitResolverRef = useRef(null);
  const nextPackboxPendingRef = useRef(false);
  const lastKeyDownTimeRef = useRef(0);
  const isBarcodeInputRef = useRef(false);

  // Keep tableRowsRef in sync so async handlers see the latest rows.
  useEffect(() => {
    tableRowsRef.current = tableRows;
  }, [tableRows]);

  // Keep imageZoomRef in sync so touch handlers read the current zoom.
  useEffect(() => {
    imageZoomRef.current = imageZoom;
  }, [imageZoom]);

  // Non-passive wheel + pinch-to-zoom listeners on the image stage.
  useEffect(() => {
    const el = imageStageRef.current;
    if (!el || !isImageModalOpen) return;

    const onWheel = (event) => {
      event.preventDefault();
      setImageZoom((current) =>
        event.deltaY < 0
          ? Math.min(MAX_IMAGE_ZOOM, current + IMAGE_ZOOM_STEP)
          : Math.max(MIN_IMAGE_ZOOM, current - IMAGE_ZOOM_STEP),
      );
    };

    let pinchStartDistance = null;
    let pinchStartZoom = 1;

    const getDistance = (touches) =>
      Math.hypot(
        touches[0].clientX - touches[1].clientX,
        touches[0].clientY - touches[1].clientY,
      );

    const onTouchStart = (event) => {
      if (event.touches.length === 2) {
        pinchStartDistance = getDistance(event.touches);
        pinchStartZoom = imageZoomRef.current;
      }
    };

    const onTouchMove = (event) => {
      if (event.touches.length !== 2 || pinchStartDistance === null) return;
      event.preventDefault();
      const scale = getDistance(event.touches) / pinchStartDistance;
      setImageZoom(
        Math.min(MAX_IMAGE_ZOOM, Math.max(MIN_IMAGE_ZOOM, pinchStartZoom * scale)),
      );
    };

    const onTouchEnd = () => {
      pinchStartDistance = null;
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd, { passive: true });

    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
    };
  }, [isImageModalOpen]);

  // Auto-updater: subscribe to push events AND pull current status on mount.
  // The pull handles the race where main emits before this effect registers the listener.
  // Error banners auto-dismiss after 8 seconds — they're non-critical.
  useEffect(() => {
    if (!window.electronAPI?.onUpdateStatus) return
    let dismissTimer = null

    const apply = (data) => {
      if (!data) return
      clearTimeout(dismissTimer)
      setUpdateInfo(data)
      if (data.status === 'error') {
        dismissTimer = setTimeout(() => setUpdateInfo(null), 8000)
      }
    }

    const cleanup = window.electronAPI.onUpdateStatus(apply)
    window.electronAPI.getUpdateStatus?.().then(apply).catch(() => {})

    return () => {
      clearTimeout(dismissTimer)
      if (typeof cleanup === 'function') cleanup()
    }
  }, [])

  useEffect(() => {
    window.electronAPI?.getVersion?.().then(setAppVersion).catch(() => {})
  }, [])


  /**
   * Resets all scan-related state to initial values.
   */
  const resetScanState = () => {
    setTableRows([]);
    setDoneDrafts({});
    setEditingDoneIdx(null);
    setActiveFilter(TABLE_FILTER.ALL);
    setCameraImage(null);
    setCurrentProduct(null);
    setStats({ total: 0, pending: 0, done: 0, completed: 0 });
    setErrors({ detection: null, ocr: null, matching: null });
    setScanStatus("Idle");
    setCurrentPackbox(1);
  };

  /**
   * Applies a state snapshot received from the backend to all relevant state fields.
   * On fresh load shows rows in the backend order; on every scan the changed row
   * bubbles to the top while the rest stay in their current display order
   * (most-recent → least-recent stacking).
   * @param {Object} data - The snapshot payload from the WebSocket message.
   */
  const applySnapshot = (data) => {
    setPicknote(data.picknote ?? "");
    setTableRows((prevRows) => {
      const next = Array.isArray(data.products) ? data.products : [];

      // Fresh load — no previous rows, show in backend order.
      if (prevRows.length === 0) return next;

      const nextByIdx = new Map(next.map((r) => [r.row_index, r]));
      const prevByIdx = new Map(prevRows.map((r) => [r.row_index, r]));

      // Detect which row changed (done_quantity increased).
      let changedIdx = null;
      for (const nr of next) {
        const idx = nr.row_index;
        const prev = prevByIdx.get(idx);
        if (
          prev &&
          (nr.done_quantity ?? 0) > (prev.done_quantity ?? 0)
        ) {
          changedIdx = idx;
          break;
        }
      }

      // Update each row's data from `next`, keeping the current display order.
      const updatedRows = prevRows.map((r) => nextByIdx.get(r.row_index) ?? r);

      if (changedIdx === null) return updatedRows; // no change, keep order

      // Move the changed row to the top.
      const targetIdx = updatedRows.findIndex((r) => r.row_index === changedIdx);
      if (targetIdx <= 0) return updatedRows; // already on top
      const [moved] = updatedRows.splice(targetIdx, 1);
      return [moved, ...updatedRows];
    });
    setCurrentProduct(data.current_product ?? null);
    if (data.current_packbox != null) setCurrentPackbox(data.current_packbox);
    setStats({
      total: data.stats?.total ?? 0,
      pending: data.stats?.pending ?? 0,
      done: data.stats?.done ?? 0,
      completed: data.stats?.completed ?? 0,
    });
    setErrors({
      detection: data.errors?.detection ?? null,
      ocr: data.errors?.ocr ?? null,
      matching: data.errors?.matching ?? null,
    });

    if (data.image) {
      setCameraImage(
        data.image.startsWith("data:")
          ? data.image
          : `data:image/jpeg;base64,${data.image}`,
      );
    }
  };

  const triggerScanFlash = (outcome) => {
    clearTimeout(scanFlashTimerRef.current);
    setScanFlash(outcome);
    scanFlashTimerRef.current = setTimeout(() => setScanFlash(null), 1200);
  };

  /**
   * Handles incoming WebSocket messages and dispatches UI updates based on message type.
   * @param {Object} data - The parsed WebSocket message object.
   * @returns {Promise<void>}
   */
  const handleWSMessage = async (data) => {
    if (data.machine_id != null) {
      setMachineId(data.machine_id);
    }

    if ("machine_config" in data) {
      setMachineConfig(data.machine_config ?? null);
    }

    if ("config_status" in data) {
      setConfigStatus(data.config_status ?? "idle");
    }

    if ("config_error" in data) {
      setConfigError(data.config_error ?? null);
    }

    if (data.type === "error") {
      setScanStatus(data.message ?? "Error");
      showErrorAlert(data.message);
      return;
    }

    if (data.type === "alert") {
      showWarningAlert(data.message);
      return;
    }

    if (data.party_name) setPartyName(data.party_name);
    if (data.store_code) setStoreCode(data.store_code);

    if (data.type === "state_snapshot") {
      applySnapshot(data);
      return;
    }

    if (data.type === "search_started") {
      setCurrentProduct(null);
      setScanStatus(`Searching ${data.picknote}`);
      return;
    }

    if (data.type === "search_result") {
      applySnapshot(data);
      setScanStatus("Picknote loaded");
      return;
    }

    if (data.type === "scan_started") {
      setCurrentProduct(null);
      if (data.image) {
        setCameraImage(
          data.image.startsWith("data:")
            ? data.image
            : `data:image/jpeg;base64,${data.image}`,
        );
      }
      setScanStatus("Detection started");
      setErrors({ detection: null, ocr: null, matching: null });
      return;
    }

    if (data.type === "scan_stage") {
      if (data.stage === "detection") setScanStatus("Detection complete");
      if (data.stage === "ocr") setScanStatus("OCR complete");
      if (data.stage === "matching") setScanStatus("Matching complete");
      return;
    }

    if (data.type === "scan_result") {
      applySnapshot(data);
      // OCR may have failed before barcode fallback succeeded — clear any
      // pipeline errors so the Error Info box doesn't show stale OCR errors
      // alongside an otherwise successful scan.
      setErrors({ detection: null, ocr: null, matching: null });
      setScanStatus("Scan complete");
      triggerScanFlash("accepted");
      return;
    }

    if (data.type === "scan_skipped") {
      applySnapshot(data);
      return;
    }

    if (data.type === "scan_exhausted") {
      applySnapshot(data);
      triggerScanFlash("rejected");
      return;
    }

    if (data.type === "packbox_changed") {
      applySnapshot(data);
      return;
    }

    if (data.type === "done_quantity_updated") {
      applySnapshot(data);
      return;
    }

    if (data.type === "submit_result") {
      const resolver = submitResolverRef.current;
      submitResolverRef.current = null;
      resolver?.(data);
      return;
    }

    if (data.type === "scan_ambiguous") {
      setScanStatus("Ambiguous — awaiting selection");
      const { isConfirmed, selectedIndex } = await showAmbiguousAlert(
        data.candidates ?? [],
        data.scanned_batch ?? "",
      );
      if (isConfirmed && selectedIndex != null) {
        const chosen = (data.candidates ?? [])[selectedIndex];
        const chosenImage = (data.preview_images ?? [])[selectedIndex];
        setCurrentProduct(chosen ?? null);
        if (chosenImage) {
          setCameraImage(
            chosenImage.startsWith("data:")
              ? chosenImage
              : `data:image/jpeg;base64,${chosenImage}`,
          );
        }
        const liveRow = tableRowsRef.current.find(
          (r) => r.row_index === chosen?.row_index,
        );
        const livePending = liveRow?.pending_quantity ?? chosen?.pending_quantity ?? 0;
        if (livePending <= 0) {
          showWarningAlert(
            `No more pending qty for product ${chosen?.product_name ?? "Unknown"}`,
          );
          setScanStatus("Ambiguous — quantity exceeded");
          sendJson({ type: "ambiguous_skipped" });
        } else {
          sendJson({
            type: "ambiguous_resolved",
            batch_number: chosen.batch_number,
            product_name: chosen.product_name,
            is_correction: Boolean(chosen._correction_batch),
          });
          setScanStatus("Scan complete");
        }
      } else {
        sendJson({ type: "ambiguous_skipped" });
        setScanStatus("Ambiguous — skipped");
      }
      return;
    }

    if (data.type === "scan_failed") {
      if (data.image) {
        setCameraImage(
          data.image.startsWith("data:")
            ? data.image
            : `data:image/jpeg;base64,${data.image}`,
        );
      }
      setCurrentProduct(null);
      const nextErrors = {
        detection: data.errors?.detection ?? null,
        ocr: data.errors?.ocr ?? null,
        matching: data.errors?.matching ?? null,
      };

      if (data.error?.type) {
        nextErrors[data.error.type] =
          data.error.message ?? nextErrors[data.error.type];
      }

      setErrors(nextErrors);
      setScanStatus(`Failed at ${data.stage ?? "unknown stage"}`);
      triggerScanFlash("rejected");
    }
  };

  const { wsStatus, connectWebSocket, sendJson, closeSocket } = useMachineSocket({
    onMessage: handleWSMessage,
  });

  /**
   * Sends a search request for the given picknote over the WebSocket and resets scan state.
   * @param {string} value - The picknote identifier to search for.
   */
  const sendSearch = useCallback(
    (value) => {
      const sent = sendJson({
        type: "search",
        picknote: value,
        machine_id: machineId,
      });

      if (!sent) {
        return;
      }

      resetScanState();
      setPartyName(null);
      setStoreCode(null);
    },
    [sendJson, machineId],
  );

  // Fire any search that was queued while the socket was still connecting.
  useEffect(() => {
    if (wsStatus === STATUS.CONNECTED && pendingSearchRef.current) {
      sendSearch(pendingSearchRef.current);
      pendingSearchRef.current = null;
    }
  }, [wsStatus, sendSearch]);

  /**
   * Validates the picknote input and initiates a search or prompts to replace the current one.
   * Connects the WebSocket if not already connected.
   * @returns {Promise<void>}
   */
  const handleSearch = async () => {
    const trimmed = picknote.trim();
    if (!trimmed){
      showErrorAlert("Picknote is required");
      return;
    }

    if (wsStatus === STATUS.CONNECTED) {
      const loadedPicknote = tableRows.length > 0;
      if (loadedPicknote) {
        const result = await confirmReplacePicknote();
        if (!result.isConfirmed) return;
      }

      sendSearch(trimmed);
      logInfo("Picknote search sent", { picknote: trimmed });
      return;
    }

    if (wsStatus === STATUS.CONNECTING) {
      showErrorAlert("Still connecting — please wait and try again.");
      return;
    }

    if (wsStatus === STATUS.ERROR) {
      showErrorAlert("Connection error — please reconnect and try again.");
      return;
    }

    pendingSearchRef.current = trimmed;
    connectWebSocket();
  };

  /**
   * Triggers a search when the Enter key is pressed in the picknote input.
   * @param {React.KeyboardEvent} event - The keyboard event from the input field.
   * @returns {Promise<void>}
   */
  const handlePicknoteKeyDown = async (event) => {
    const now = Date.now();

    if (event.key !== "Enter") {
      const gap = now - lastKeyDownTimeRef.current;
      if (lastKeyDownTimeRef.current > 0 && gap < 50) {
        isBarcodeInputRef.current = true;
      } else if (gap > 300) {
        isBarcodeInputRef.current = false;
      }
      lastKeyDownTimeRef.current = now;
      return;
    }

    event.preventDefault();

    if (isBarcodeInputRef.current) {
      isBarcodeInputRef.current = false;
      lastKeyDownTimeRef.current = 0;
      setPicknote("");
      return;
    }

    await handleSearch();
  };

  /**
   * Clears the picknote input, resets all scan state, and refocuses the input field.
   */
  const handleLogout = () => {
    closeSocket();
    handleClear();
  };

  const handleClear = async () => {
    if (tableRows.length > 0) {
      const { isConfirmed } = await confirmClear();
      if (!isConfirmed) return;
    }
    pendingSearchRef.current = null;
    setPicknote("");
    resetScanState();
    setPartyName(null);
    setStoreCode(null);
    inputRef.current?.focus();
  };

  /**
   * Submits the current picknote and its product rows to the backend API.
   * Resets state and shows a success alert on completion.
   * @returns {Promise<void>}
   */
  const handleSubmit = async () => {
    if (isSubmitting) return;
    // Commit any Done Qty edit still in progress before reading the rows.
    if (document.activeElement instanceof HTMLElement) {
      document.activeElement.blur();
    }
    const { isConfirmed } = await confirmSubmit(picknote);
    if (!isConfirmed) return;
    setIsSubmitting(true);
    try {
      // Group submission lines by (product_name, batch_number). qty_done comes
      // from each row's done_quantity (scan increments and manual edits alike);
      // scan_log is only used for packbox attribution. Read rows via the ref so
      // the blur-committed optimistic update above is visible.
      const byProduct = new Map();
      for (const row of tableRowsRef.current) {
        const n = row.product_name ?? "";
        if (!byProduct.has(n)) byProduct.set(n, []);
        byProduct.get(n).push(row);
      }

      const submitProducts = [];
      for (const rows of byProduct.values()) {
        const template = rows[0];

        const batchGroups = new Map();
        for (const r of rows) {
          const key = r.batch_number ?? "";
          if (!batchGroups.has(key)) {
            batchGroups.set(key, {
              qty: 0,
              batchQty: 0,
              packboxes: [],
              scanned: false,
            });
          }
          const g = batchGroups.get(key);
          g.qty += r.done_quantity ?? 0;
          g.batchQty += r.batch_quantity ?? 0;
          for (const s of r.scan_log ?? []) {
            g.packboxes.push(s.packbox);
            g.scanned = true;
          }
        }

        for (const [batch_number, g] of batchGroups.entries()) {
          // Include anything with qty > 0, plus scanned-then-zeroed lines on
          // real picklist batches so Odoo's qty_done is explicitly reset to 0.
          // Skip untouched rows and zeroed batch_quantity==0 groups — the
          // backend would INSERT a junk all-zero stock_move_line for those.
          if (g.qty <= 0 && !(g.scanned && g.batchQty > 0)) continue;
          const uniqueBoxes = [...new Set(g.packboxes)].sort((a, b) => a - b);
          submitProducts.push({
            product_name: template.product_name,
            product_code: template.product_code,
            batch_number,
            qty_done: g.qty,
            box_number: uniqueBoxes.length
              ? uniqueBoxes.join(",")
              : String(currentPackbox),
            scaning_status: "OCR",
          });
        }
      }

      const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const resultPromise = new Promise((resolve) => {
        submitResolverRef.current = resolve;
      });
      const sent = sendJson({
        type: "submit",
        request_id: requestId,
        picknote,
        machine_id: machineId,
        party_name: partyName,
        store_code: storeCode,
        products: submitProducts,
      });
      if (!sent) {
        submitResolverRef.current = null;
        logError("Picknote submit failed: WebSocket not connected", { picknote });
        await showErrorAlert("Not connected — submit failed. Please try again.");
        return;
      }
      const result = await resultPromise;
      if (!result?.ok) {
        logError("Picknote submit failed", { picknote, detail: result?.detail });
        await showErrorAlert(result?.detail ?? "Submit failed");
        return;
      }
      setPicknote("");
      resetScanState();
      setPartyName(null);
      setStoreCode(null);
      logInfo("Picknote submitted successfully", { picknote, machineId });
      await showSuccessAlert(`Picknote ${picknote} submitted successfully`);
    } catch (e) {
      logError("Picknote submit network error", { picknote, error: e?.message });
      await showErrorAlert("Network error — submit failed. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  };

  /**
   * Commits a manually edited Done Qty for a row: clamps the draft value to
   * [0, batch_quantity] (any value >= 0 when batch_quantity is 0), applies an
   * optimistic local update, and sends the change to the machine so it is
   * persisted and broadcast back in a snapshot.
   * @param {Object} row - The table row whose Done Qty draft should be committed.
   */
  const commitDoneQty = (row) => {
    const idx = row.row_index;
    const raw = doneDrafts[idx];
    setEditingDoneIdx(null);
    setDoneDrafts((drafts) => {
      const next = { ...drafts };
      delete next[idx];
      return next;
    });
    if (doneEditCancelledRef.current) {
      doneEditCancelledRef.current = false;
      return;
    }
    if (raw == null) return;
    const parsed = Number.parseInt(raw, 10);
    if (Number.isNaN(parsed)) return; // empty/invalid input — revert
    const batchQty = row.batch_quantity ?? 0;
    const clamped =
      batchQty > 0
        ? Math.min(Math.max(parsed, 0), batchQty)
        : Math.max(parsed, 0);
    if (clamped === (row.done_quantity ?? 0)) return;
    // Optimistic update so the cell doesn't flash the old value before the
    // snapshot round-trips, and so the bubble-to-top logic sees no increase.
    setTableRows((rows) =>
      rows.map((r) =>
        r.row_index === idx
          ? {
              ...r,
              done_quantity: clamped,
              pending_quantity:
                batchQty > 0 ? Math.max(batchQty - clamped, 0) : 0,
            }
          : r,
      ),
    );
    sendJson({
      type: "update_done_quantity",
      row_index: idx,
      done_quantity: clamped,
    });
  };

  /**
   * Closes the WebSocket connection and clears all picknote and scan state.
   */
  const handleNextPackbox = async () => {
    if (!picknote || tableRows.length === 0) return;
    if (nextPackboxPendingRef.current) return;
    nextPackboxPendingRef.current = true;
    try {
      const { isConfirmed } = await confirmNextPackbox(currentPackbox);
      if (!isConfirmed) return;
      sendJson({ type: "next_packbox" });
    } finally {
      nextPackboxPendingRef.current = false;
    }
  };



  /**
   * Opens the full-screen image modal if a camera image is available, resetting zoom to 1.
   */
  const openImageModal = () => {
    if (!cameraImage) return;
    setImageZoom(1);
    setIsImageModalOpen(true);
  };

  /**
   * Closes the full-screen image modal and resets zoom to 1.
   */
  const closeImageModal = () => {
    setIsImageModalOpen(false);
    setImageZoom(1);
  };

  /**
   * Increases the image zoom level by one step, up to the maximum allowed zoom.
   */
  const handleZoomIn = () => {
    setImageZoom((current) => Math.min(MAX_IMAGE_ZOOM, current + IMAGE_ZOOM_STEP));
  };

  /**
   * Decreases the image zoom level by one step, down to the minimum allowed zoom.
   */
  const handleZoomOut = () => {
    setImageZoom((current) => Math.max(MIN_IMAGE_ZOOM, current - IMAGE_ZOOM_STEP));
  };

  const filteredRows = filterTableRows(tableRows, activeFilter);

  // Count individual rows (line items) that are fully done.
  // Intentionally row-level so two completed DOLO 650 rows count as 2.
  const completedProducts = tableRows.filter(
    (r) => (r.done_quantity ?? 0) > 0 && (r.done_quantity ?? 0) >= (r.batch_quantity ?? 0),
  ).length;

  const wsColor =
    {
      idle: "#9ca3af",
      connecting: "#f59e0b",
      connected: "#16a34a",
      error: "#dc2626",
    }[wsStatus] ?? "#9ca3af";

  const wsLabel =
    {
      idle: "Disconnected",
      connecting: "Connecting…",
      connected: "Connected",
      error: "Error",
    }[wsStatus] ?? "Disconnected";

  const configEntries = machineConfig
    ? [
        ["Machine ID", machineConfig.machine_id],
        ["Camera Serial", machineConfig.camera_serial],
        [
          "Exposure Time",
          machineConfig.exposure_time_us != null
            ? `${machineConfig.exposure_time_us.toLocaleString()} µs (${(machineConfig.exposure_time_us / 1000).toFixed(1)} ms)`
            : "—",
        ],
        ["Conveyor IP", machineConfig.conveyor_ip],
        ["Conveyor Port", machineConfig.conveyor_port],
        ["Start Conveyor", machineConfig.start_conveyor],
        ["Accept Conveyor", machineConfig.accept_conveyor],
        ["Reject Conveyor", machineConfig.reject_conveyor],
      ]
    : [["Machine ID", machineId]];

  return (
    <div className="app-root">
      {scanFlash && (
        <>
          <div className={`scan-flash-line scan-flash-line--top scan-flash-line--${scanFlash}`} />
          <div className={`scan-flash-line scan-flash-line--bottom scan-flash-line--${scanFlash}`} />
          <div className={`scan-flash-line scan-flash-line--left scan-flash-line--${scanFlash}`} />
          <div className={`scan-flash-line scan-flash-line--right scan-flash-line--${scanFlash}`} />
        </>
      )}
      {updateInfo && updateInfo.status === 'checking' && (
        <div className="update-banner update-banner--info">
          Checking for updates…
        </div>
      )}
      {updateInfo && updateInfo.status === 'available' && (
        <div className="update-banner update-banner--info">
          Update v{updateInfo.version} available.{' '}
          <button
            className="update-install-btn"
            onClick={() => window.electronAPI?.downloadUpdate()}
          >
            Download
          </button>
        </div>
      )}
      {updateInfo && updateInfo.status === 'downloading' && (
        <div className="update-banner update-banner--info">
          Downloading update… {updateInfo.percent}%
        </div>
      )}
      {updateInfo && updateInfo.status === 'downloaded' && (
        <div className="update-banner update-banner--ready">
          Update v{updateInfo.version} ready.{' '}
          <button
            className="update-install-btn"
            onClick={() => window.electronAPI?.installUpdate()}
          >
            Restart &amp; Install
          </button>
        </div>
      )}
      {updateInfo && updateInfo.status === 'error' && (
        <div className="update-banner update-banner--error">
          ⚠ {updateInfo.message}
        </div>
      )}
      <header className="app-title-bar">
        <span className="app-title-text">
          Picknote Scanner System
          {appVersion && <span className="app-title-version">v{appVersion}</span>}
        </span>

        <div className="app-ws-row">
          <div className="app-ws-dot" style={{ backgroundColor: wsColor }} />
          {wsStatus === STATUS.IDLE ? (
            <button
              type="button"
              className="app-connect-btn"
              onClick={connectWebSocket}
            >
              Connect
            </button>
          ) : (
            <span className="app-ws-text">{wsLabel}</span>
          )}
        </div>
      </header>

      <main className="app-main">
        <section className="app-left-col">
          <InfoCard title="Picknote Info">
            <InfoRow label="Party Name" value={partyName} valueClassName="app-info-value-scroll" />
            <InfoRow label="Store Code" value={storeCode} />
          </InfoCard>

          <div className="app-spacer" />

          <InfoCard title="Product Info">
            <InfoRow label="Product Name" value={currentProduct?.product_name} />
            <InfoRow label="Product Code" value={currentProduct?.product_code} />
            <InfoRow label="Batch Number" value={currentProduct?.batch_number} />
            <InfoRow label="EXP Date" value={currentProduct?.expiry_date} />
            <InfoRow
              label="MRP"
              value={currentProduct?.mrp != null ? `₹${currentProduct.mrp}` : null}
            />
          </InfoCard>

          <div className="app-bottom-camera">
            {cameraImage ? (
              <button
                type="button"
                className="app-camera-button"
                onClick={openImageModal}
              >
                <img src={cameraImage} alt="Camera" className="app-camera-img" />
              </button>
            ) : (
              <div className="app-camera-placeholder" />
            )}
          </div>

          <button
            type="button"
            className="app-packbox-btn"
            onClick={handleNextPackbox}
            disabled={!picknote || tableRows.length === 0}
            aria-label={`Pack box ${currentPackbox}, click for next box`}
          >
            <span className="app-packbox-label">PACK BOX</span>
            <span className="app-packbox-number">{currentPackbox}</span>
            <span className="app-packbox-sublabel">Tap for Next Box</span>
          </button>
        </section>

        <section className="app-center-col">
          <div className="app-top-row">
            <div className="app-top-row-left">
              <div className="app-search-row">
                <span className="app-search-label">Picknote:</span>
                <input
                  ref={inputRef}
                  className="app-input"
                  placeholder="e.g. PN-0001"
                  value={picknote}
                  onChange={(event) => setPicknote(event.target.value)}
                  onKeyDown={handlePicknoteKeyDown}
                />
                <button
                  type="button"
                  className="app-btn app-btn-search"
                  onClick={handleSearch}
                >
                  Search
                </button>
              </div>

              <div className="app-stats-row">
                <StatBox
                  label="Total Qty"
                  value={stats.total}
                  isActive={activeFilter === TABLE_FILTER.ALL}
                  onClick={() => setActiveFilter(TABLE_FILTER.ALL)}
                  bg="#eef2ff"
                  labelColor="#3730a3"
                  valueColor="#4f46e5"
                  border="#c7d2fe"
                />
                <StatBox
                  label="Pending Qty"
                  value={stats.pending}
                  isActive={activeFilter === TABLE_FILTER.PENDING}
                  onClick={() => setActiveFilter(TABLE_FILTER.PENDING)}
                  bg="#fff7ed"
                  labelColor="#92400e"
                  valueColor="#d97706"
                  border="#fcd34d"
                />
                <StatBox
                  label="Done Qty"
                  value={stats.done}
                  isActive={activeFilter === TABLE_FILTER.DONE}
                  onClick={() => setActiveFilter(TABLE_FILTER.DONE)}
                  bg="#eff6ff"
                  labelColor="#1e40af"
                  valueColor="#2563eb"
                  border="#bfdbfe"
                />
                <StatBox
                  label="Completed"
                  value={completedProducts}
                  isActive={activeFilter === TABLE_FILTER.COMPLETED}
                  onClick={() => setActiveFilter(TABLE_FILTER.COMPLETED)}
                  bg="#fdf2f8"
                  labelColor="#86198f"
                  valueColor="#a21caf"
                  border="#f0abfc"
                />
              </div>
            </div>

            <InfoCard title="Error Info">
              <ErrorRow label="Detection Error" value={errors.detection} />
              <ErrorRow label="OCR Error" value={errors.ocr} />
              <ErrorRow label="Matching Error" value={errors.matching} />
            </InfoCard>
          </div>

          <div className="app-table-wrap">
            <table className="app-table">
              <thead className="app-table-head">
                <tr>
                  {TABLE_HEADERS.map((header) => (
                    <th key={header} className="app-th">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filteredRows.length === 0 ? (
                  <tr>
                    <td colSpan={TABLE_HEADERS.length} className="app-empty-td">
                      No records
                    </td>
                  </tr>
                ) : (
                  filteredRows.map((row, index) => (
                    <tr
                      key={row.row_index ?? index}
                      className={index % 2 === 0 ? "app-row-even" : "app-row-odd"}
                    >
                      <td className="app-td app-td-product-name">{row.product_name ?? "—"}</td>
                      <td className="app-td">{row.product_code ?? "—"}</td>
                      <td className="app-td">{row.batch_number ?? "—"}</td>
                      <td className="app-td app-td-batch-correction">
                        {row.batch_corrections?.length
                          ? (() => {
                              const counts = {};
                              for (const b of row.batch_corrections) counts[b] = (counts[b] ?? 0) + 1;
                              return Object.entries(counts).map(([b, n]) => `${b}: ${n}`).join(", ");
                            })()
                          : "—"}
                      </td>
                      <td className="app-td">{row.batch_quantity ?? "—"}</td>
                      <td className="app-td">
                        {editingDoneIdx === row.row_index ? (
                          <input
                            type="number"
                            min="0"
                            inputMode="numeric"
                            autoFocus
                            className="app-input app-done-qty-input"
                            value={doneDrafts[row.row_index] ?? String(row.done_quantity ?? "")}
                            onFocus={(event) => event.target.select()}
                            onChange={(event) =>
                              setDoneDrafts((drafts) => ({
                                ...drafts,
                                [row.row_index]: event.target.value,
                              }))
                            }
                            onBlur={() => commitDoneQty(row)}
                            onKeyDown={(event) => {
                              if (event.key === "Enter") event.currentTarget.blur();
                              if (event.key === "Escape") {
                                doneEditCancelledRef.current = true;
                                event.currentTarget.blur();
                              }
                            }}
                          />
                        ) : (
                          <span
                            className="app-done-qty-value"
                            onClick={() => {
                              setDoneDrafts((drafts) => ({
                                ...drafts,
                                [row.row_index]: String(row.done_quantity ?? 0),
                              }));
                              setEditingDoneIdx(row.row_index);
                            }}
                          >
                            {row.done_quantity ?? "—"}
                          </span>
                        )}
                      </td>
                      <td className="app-td app-td-packbox">
                        {row.scan_log?.length
                          ? (() => {
                              const counts = {};
                              for (const s of row.scan_log) {
                                const k = s.packbox;
                                counts[k] = (counts[k] ?? 0) + 1;
                              }
                              return Object.entries(counts)
                                .map(([p, n]) => `Box ${p}: ${n}`)
                                .join(", ");
                            })()
                          : "—"}
                      </td>
                      <td className="app-td">{row.pending_quantity ?? "—"}</td>
                      <td className="app-td">{row.expiry_date ?? "—"}</td>
                      <td className="app-td">{row.mrp ?? "—"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

      </main>

      <div className="app-btn-row">
        <button
          type="button"
          className="app-btn app-btn-clear"
          onClick={handleClear}
        >
          Clear
        </button>
        <button
          type="button"
          className="app-btn app-btn-submit"
          disabled={!picknote || tableRows.length === 0 || isSubmitting}
          onClick={handleSubmit}
        >
          Submit
        </button>
        <button
          type="button"
          className="app-btn app-btn-logout"
          onClick={handleLogout}
        >
          Log Out
        </button>
      </div>

      <button
        type="button"
        className="app-config-trigger"
        aria-label="Show machine config"
        aria-expanded={isConfigPopupOpen}
        onClick={() => setIsConfigPopupOpen((current) => !current)}
      >
        i
      </button>

      {isConfigPopupOpen ? (
        <div className="app-config-popup" role="dialog" aria-label="Machine config">
          <div className="app-config-popup-header">
            <span>Machine Config</span>
            <button
              type="button"
              className="app-config-close"
              aria-label="Close machine config"
              onClick={() => setIsConfigPopupOpen(false)}
            >
              ×
            </button>
          </div>

          <div className="app-config-meta">
            <span>Status: {configStatus}</span>
          </div>

          {configError ? (
            <div className="app-config-error">{configError}</div>
          ) : null}

          <div className="app-config-list">
            {configEntries.map(([label, value]) => (
              <div key={label} className="app-config-row">
                <span className="app-config-label">{label}</span>
                <span className="app-config-value">{value ?? "—"}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {isImageModalOpen && cameraImage ? (
        <div className="app-image-modal" role="dialog" aria-label="Camera image preview">
          <div className="app-image-toolbar">
            <div className="app-image-zoom-group">
              <button
                type="button"
                className="app-image-toolbar-btn"
                onClick={handleZoomOut}
                disabled={imageZoom <= MIN_IMAGE_ZOOM}
              >
                -
              </button>
              <span className="app-image-zoom-text">{Math.round(imageZoom * 100)}%</span>
              <button
                type="button"
                className="app-image-toolbar-btn"
                onClick={handleZoomIn}
                disabled={imageZoom >= MAX_IMAGE_ZOOM}
              >
                +
              </button>
            </div>

            <button
              type="button"
              className="app-image-close"
              aria-label="Close image preview"
              onClick={closeImageModal}
            >
              ×
            </button>
          </div>

          <div className="app-image-stage" ref={imageStageRef}>
            <img
              src={cameraImage}
              alt="Camera full preview"
              className="app-image-modal-img"
              style={{ transform: `scale(${imageZoom})` }}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}
