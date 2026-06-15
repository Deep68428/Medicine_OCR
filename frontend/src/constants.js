export const STATUS = {
  IDLE: "idle",
  CONNECTING: "connecting",
  CONNECTED: "connected",
  ERROR: "error",
};

export const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8001/ws";
export const CONNECT_TIMEOUT_MS = 4000;
export const RECONNECT_DELAY_MS = 3000;

export const TABLE_HEADERS = [
  "Product Name",
  "Code",
  "Batch No",
  "Batch Correction",
  "Batch Qty",
  "Done Qty",
  "Pack Boxes",
  "Pending",
  "EXP Date",
  "MRP",
];

export const TABLE_FILTER = {
  ALL: "all",
  PENDING: "pending",
  DONE: "done",
  COMPLETED: "completed",
};
