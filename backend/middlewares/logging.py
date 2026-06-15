import time

from fastapi import Request
from loguru import logger


async def logging_middleware(request: Request, call_next):
    start_time = time.time()

    # Get client IP address (respect proxies via X-Forwarded-For)
    client_ip = request.headers.get(
        "X-Forwarded-For", request.client.host if request.client else "unknown"
    )
    # X-Forwarded-For can contain multiple IPs, take the first one
    client_ip = client_ip.split(",")[0].strip() if client_ip else "unknown"

    # Add IP to logging context so it appears in all logs for this request
    with logger.contextualize(client_ip=client_ip):
        logger.info(f"➡️ {request.method} {request.url.path}")

        try:
            response = await call_next(request)
        except Exception as e:
            logger.exception(
                f"🔥 Unhandled error on {request.method} {request.url.path}"
            )
            raise e

        duration = (time.time() - start_time) * 1000

        logger.info(
            f"⬅️ {request.method} {request.url.path} | "
            f"{response.status_code} | {duration:.2f} ms"
        )

        return response
