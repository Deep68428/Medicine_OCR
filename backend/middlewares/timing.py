import time

from fastapi import Request
from loguru import logger

SLOW_REQUEST_THRESHOLD_MS = 3000


async def timing_middleware(request: Request, call_next):
    start_time = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = (time.perf_counter() - start_time) * 1000

    # Attach header for debugging / monitoring
    response.headers["X-Process-Time-ms"] = f"{elapsed_ms:.2f}"

    if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
        logger.bind(ai=True, agent="system").warning(
            f"Slow request: {request.method} {request.url.path} "
            f"took {elapsed_ms:.2f} ms"
        )
    else:
        logger.debug(
            f"{request.method} {request.url.path} " f"took {elapsed_ms:.2f} ms"
        )

    return response
