from __future__ import annotations

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from starlette.websockets import WebSocketState

from services.state import MachineState


async def connect_websocket(state: MachineState, websocket: WebSocket) -> None:
    """Accept a new WebSocket connection, closing any pre-existing one first.

    Args:
        state: The current MachineState whose websocket field will be updated.
        websocket: The incoming WebSocket connection to accept.
    """
    if (
        state.websocket is not None
        and state.websocket is not websocket
        and state.websocket.application_state == WebSocketState.CONNECTED
    ):
        logger.info("Closing previous frontend websocket before accepting a new one")
        await state.websocket.close(code=1000)
    await websocket.accept()
    state.websocket = websocket
    logger.info("Websocket connection established with frontend")


def disconnect_websocket(
    state: MachineState, websocket: WebSocket | None = None
) -> None:
    """Clear the active websocket from state, ignoring stale disconnection events.

    Args:
        state: The current MachineState to update.
        websocket: The disconnecting WebSocket. If it does not match the one
            currently stored in state, the call is silently ignored.
    """
    if websocket is not None and state.websocket is not websocket:
        logger.info("Ignoring disconnect from stale websocket connection")
        return
    logger.info("Websocket connection closed; resetting websocket state")
    state.websocket = None


async def send_payload(state: MachineState, payload: dict) -> None:
    """Send a JSON payload to the currently connected frontend WebSocket.

    Handles disconnection and close-in-progress errors gracefully by clearing
    the websocket reference rather than propagating the exception.

    Args:
        state: The current MachineState providing the active websocket.
        payload: A JSON-serialisable dict to transmit.
    """
    if state.websocket is None:
        logger.warning("Attempted to send payload without an active websocket")
        return

    try:
        logger.debug("Sending payload to frontend: {}", payload.get("type"))
        await state.websocket.send_json(payload)
    except WebSocketDisconnect:
        logger.info("Websocket disconnected while sending payload")
        state.websocket = None
    except RuntimeError as exc:
        if "close message has been sent" in str(exc):
            logger.info("Websocket already closing; dropping payload")
            state.websocket = None
            return
        logger.exception("Unexpected runtime error while sending payload")
        raise
