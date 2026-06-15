import asyncio
import socket
from loguru import logger


class ConveyorSocket:
    def __init__(
        self, ip: str, port: int, start_cmd: str, accept_cmd: str, reject_cmd: str
    ):
        self.ip = ip
        self.port = port
        self.start_cmd = start_cmd
        self.accept_cmd = accept_cmd
        self.reject_cmd = reject_cmd
        self.sock: socket.socket | None = None

    def connect(self, timeout: float = 5.0):
        """Connect once at startup and send start command. Fails fast if unreachable."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect((self.ip, self.port))
            self.sock.settimeout(None)
            logger.info("Conveyor TCP connected to {}:{}", self.ip, self.port)
            logger.info("Sending conveyor start command: {}", self.start_cmd)
            self.send(self.start_cmd)
        except Exception as e:
            self.sock = None
            logger.error(
                "Conveyor TCP connect failed ({}:{}) — conveyor disabled: {}",
                self.ip,
                self.port,
                e,
            )

    def send(self, cmd: str):
        if self.sock:
            try:
                self.sock.sendall(cmd.encode())
            except Exception as e:
                logger.error("Conveyor send failed (cmd={!r}): {}", cmd, e)
        else:
            logger.error("Conveyor command {!r} dropped — socket not connected", cmd)

    def move_accept(self):
        logger.info("Sending conveyor accept command: {}", self.accept_cmd)
        self.send(self.accept_cmd)

    def move_reject(self):
        logger.info("Sending conveyor reject command: {}", self.reject_cmd)
        self.send(self.reject_cmd)

    def start_listening(self, loop: asyncio.AbstractEventLoop):
        """Register an asyncio reader on the TCP socket.

        Calls on_trigger() each time the conveyor sends 't' — no while-loop needed.
        """
        if self.sock is None:
            logger.warning("Conveyor: cannot start_listening — socket not connected")
            return
        loop.add_reader(self.sock.fileno(), self._handle_recv)
        logger.info("Conveyor: listening for software trigger signal 't'")

    def _handle_recv(self):
        try:
            data = self.sock.recv(64)
            if not data:
                logger.warning("Conveyor TCP connection closed by peer")
                return
            if len(data) == 1:
                if b"t" in data:
                    logger.bind(source="controller").info(
                        "Conveyor sent trigger 't' — firing camera capture"
                    )
            if len(data) > 1:
                logger.bind(source="controller").info(
                    "log from controller: {}", data.decode("utf-8").strip()
                )

            else:
                logger.bind(source="controller").warning(
                    "Got 0 length data from controller"
                )

        except Exception as e:
            logger.bind(source="controller").error("Conveyor recv error: {}", e)

    def stop_listening(self, loop: asyncio.AbstractEventLoop):
        if self.sock:
            try:
                loop.remove_reader(self.sock.fileno())
            except Exception:
                pass

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error("Conveyor socket close failed: {}", e)
