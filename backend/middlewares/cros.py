from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


ALLOWED_ORIGINS = ["*"]


def setup_cors(app: FastAPI) -> None:
    """
    Attach CORS middleware to the FastAPI app.

    Keeping this in the middlewares package keeps configuration
    consistent with logging / request_id / timing middlewares.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
