import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from concurrent.futures import ThreadPoolExecutor
from scripts.run_cam import make_camera_runner, _CAMERA_AVAILABLE
from services.barcode_scanner import BarcodeScanner
from services.controller import MachineController
from services.recognition import warmup_pipeline
from scripts.conv_control import ConveyorSocket


def create_app() -> FastAPI:
    """Create and configure the FastAPI application with all routes and lifespan hooks.
    Returns:
        A configured FastAPI instance ready to be served.
    """
    controller = MachineController()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize the controller on startup and shut it down on teardown."""
        await controller.initialize_from_server()
        loop = asyncio.get_event_loop()
        if _CAMERA_AVAILABLE:
            # Hardware camera present — warm up models in background so first scan isn't slow.
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="warmup")
            loop.run_in_executor(_executor, warmup_pipeline)
        else:
            logger.info(
                "No hardware camera — skipping model warmup (manual /trigger mode)"
            )

        machine_config = controller.state.machine_config or {}
        conveyor_ip = machine_config.get("conveyor_ip", "")
        conveyor_port = int(machine_config.get("conveyor_port") or 0)
        start_cmd = machine_config.get("start_conveyor", "")
        accept_cmd = machine_config.get("accept_conveyor", "")
        reject_cmd = machine_config.get("reject_conveyor", "")

        conveyor = None
        if conveyor_ip and conveyor_port:
            conveyor = ConveyorSocket(
                conveyor_ip, conveyor_port, start_cmd, accept_cmd, reject_cmd
            )
            conveyor.connect()
            if conveyor.sock is None:
                logger.warning(
                    "Conveyor connection failed — conveyor moves will be skipped"
                )
                conveyor = None
        else:
            logger.warning(
                "Conveyor IP/port not configured — conveyor moves will be skipped"
            )

        controller.conveyor = conveyor
        controller.pipeline.conveyor = conveyor

        cam = make_camera_runner(controller, loop)
        cam.start_async()

        if conveyor is not None and _CAMERA_AVAILABLE:
            conveyor.start_listening(loop)

        scanner = BarcodeScanner(
            on_barcode=controller.process_barcode_scan,
            loop=loop,
        )
        scanner.start_async()

        yield

        cam.stop()
        scanner.stop()
        if conveyor is not None:
            conveyor.close()
        await controller.close()

    app = FastAPI(title="Medicine Box Machine Controller", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time bidirectional communication with the frontend.
        Sends an initial state snapshot on connect, then continuously dispatches
        incoming messages to the controller until the client disconnects.
        """
        await controller.connect(websocket)
        try:
            await controller.send_snapshot(event_type="state_snapshot")

            while True:
                try:
                    payload = await websocket.receive_json()
                    await controller.handle_message(payload)
                except WebSocketDisconnect:
                    raise
                except RuntimeError as exc:
                    if 'Need to call "accept" first.' in str(exc):
                        logger.info("Stale websocket handler detected; closing loop")
                        break
                    raise
                except Exception as exc:
                    logger.exception("Machine websocket request failed")
                    try:
                        await controller.send(
                            {
                                "type": "error",
                                "message": f"Machine controller error: {exc}",
                            }
                        )
                    except WebSocketDisconnect:
                        raise
        except WebSocketDisconnect:
            logger.info("Frontend disconnected from machine controller")
        finally:
            controller.disconnect(websocket)

    class FrontendLogEntry(BaseModel):
        level: str = "info"
        message: str
        source: str = "frontend"
        context: dict[str, Any] = {}

    @app.post("/api/frontend-log", status_code=204)
    async def frontend_log_endpoint(entry: FrontendLogEntry):
        """Accept structured log entries from the Electron/React frontend."""
        level = entry.level.upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = "INFO"
        logger.bind(source=entry.source, **entry.context).log(level, entry.message)

    @app.post("/trigger")
    async def trigger_endpoint(
        image: UploadFile = File(...),
        picknote: str = Form(""),
        machine_id: int = Form(None),
    ):
        """Manual image trigger — used in dev/test when hardware camera is absent.
        Send a multipart/form-data POST with:
          image     — the captured frame (JPEG/PNG)
          picknote  — current picknote (optional, defaults to loaded one)
          machine_id — machine id override (optional)
        """
        image_bytes = await image.read()
        result = await controller.process_camera_trigger(
            image_bytes,
            picknote=picknote,
            machine_id=machine_id,
        )
        return result

    return app
