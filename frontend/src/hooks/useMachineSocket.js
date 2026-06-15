import { useEffect, useRef, useState } from "react";

import {
  CONNECT_TIMEOUT_MS,
  RECONNECT_DELAY_MS,
  STATUS,
  WS_URL,
} from "../constants";
import { logError, logInfo } from "../utils/logger";

/**
 * Hook that manages a WebSocket connection to the machine backend.
 * Automatically connects on mount, reconnects on disconnect, and parses incoming JSON messages.
 * @param {Object} options
 * @param {function(Object): void} options.onMessage - Callback invoked with each parsed WebSocket message.
 * @returns {{ wsStatus: string, connectWebSocket: function, sendJson: function(Object): boolean, closeSocket: function }}
 */
export function useMachineSocket({ onMessage }) {
  const [wsStatus, setWsStatus] = useState(STATUS.IDLE);

  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const connectTimeoutRef = useRef(null);
  const shouldReconnectRef = useRef(true);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const clearConnectionTimers = () => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    if (connectTimeoutRef.current) {
      window.clearTimeout(connectTimeoutRef.current);
      connectTimeoutRef.current = null;
    }
  };

  const connectWebSocket = () => {
    shouldReconnectRef.current = true;
    const currentState = wsRef.current?.readyState;
    if (
      currentState === WebSocket.OPEN ||
      currentState === WebSocket.CONNECTING
    ) {
      return;
    }

    clearConnectionTimers();
    setWsStatus(STATUS.CONNECTING);

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    connectTimeoutRef.current = window.setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      clearConnectionTimers();
      setWsStatus(STATUS.CONNECTED);
      logInfo("WebSocket connected", { url: WS_URL });
    };

    ws.onclose = () => {
      clearConnectionTimers();
      const isCurrentSocket = wsRef.current === ws;
      if (isCurrentSocket) {
        wsRef.current = null;
        setWsStatus(STATUS.IDLE);
        logInfo("WebSocket disconnected");
        if (shouldReconnectRef.current && !reconnectTimerRef.current) {
          reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            connectWebSocket();
          }, RECONNECT_DELAY_MS);
        }
      }
    };

    ws.onerror = () => {
      if (wsRef.current === ws) {
        setWsStatus(STATUS.ERROR);
        logError("WebSocket error", { url: WS_URL });
      }
    };

    ws.onmessage = (event) => {
      try {
        onMessageRef.current(JSON.parse(event.data));
      } catch (err) {
        console.error("WebSocket: failed to parse message", err, event.data);
      }
    };
  };

  const sendJson = (payload) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(payload));
      return true;
    }

    connectWebSocket();
    return false;
  };

  const closeSocket = () => {
    shouldReconnectRef.current = false;
    wsRef.current?.close();
  };

  useEffect(() => {
    connectWebSocket();

    return () => {
      shouldReconnectRef.current = false;
      clearConnectionTimers();
      wsRef.current?.close();
    };
  }, []);

  return {
    wsStatus,
    connectWebSocket,
    sendJson,
    closeSocket,
  };
}
