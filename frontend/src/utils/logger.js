/**
 * Structured logger for the React/Electron frontend.
 * Sends log entries to machine_code's /api/frontend-log (always local).
 * On failure: buffers in localStorage (max 200 entries), drains on next success.
 */

const ENDPOINT = (import.meta.env.VITE_MACHINE_URL || "http://localhost:8001") + "/api/frontend-log";
const BUFFER_KEY = "frontend_log_buffer";
const BUFFER_MAX = 200;

function readBuffer() {
  try {
    return JSON.parse(localStorage.getItem(BUFFER_KEY) || "[]");
  } catch {
    return [];
  }
}

function writeBuffer(entries) {
  try {
    localStorage.setItem(BUFFER_KEY, JSON.stringify(entries.slice(-BUFFER_MAX)));
  } catch {
    // localStorage full — oldest entries silently dropped by slice
  }
}

async function flush() {
  const buf = readBuffer();
  if (!buf.length) return;
  const remaining = [];
  for (const entry of buf) {
    if (!(await sendOne(entry, false))) {
      remaining.push(entry);
      break; // still offline — stop draining
    }
  }
  writeBuffer(remaining);
}

async function sendOne(entry, flushOnSuccess = true) {
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
      signal: AbortSignal.timeout(3000),
    });
    if (res.ok && flushOnSuccess) await flush();
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * @param {"debug"|"info"|"warning"|"error"|"critical"} level
 * @param {string} message
 * @param {Record<string, unknown>} [context={}]
 */
export async function log(level, message, context = {}) {
  const entry = {
    level,
    message,
    source: "frontend",
    context: { timestamp: new Date().toISOString(), ...context },
  };
  if (!(await sendOne(entry))) {
    writeBuffer([...readBuffer(), entry]);
  }
}

export const logInfo  = (msg, ctx) => log("info",    msg, ctx);
export const logWarn  = (msg, ctx) => log("warning", msg, ctx);
export const logError = (msg, ctx) => log("error",   msg, ctx);
