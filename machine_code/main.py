import multiprocessing

multiprocessing.freeze_support()  # noqa: E402
import os  # noqa: E402
import uvicorn  # noqa: E402

from api.app import create_app  # noqa: E402
from core.logging import setup_logging  # noqa: E402


# Cap OpenVINO / OpenMP threads before any ML library is imported.
# Without this, OpenVINO grabs every CPU core for model compilation and inference.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENVINO_CPU_THREADS_NUM", "2")


setup_logging()
app = create_app()


if __name__ == "__main__":
    # Pass the app object directly — the string form ("main:app") makes uvicorn
    # spawn subprocess workers which re-executes the frozen binary and causes a crash loop.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
    )
