import hashlib
import shutil
from pathlib import Path
from urllib.parse import unquote

import requests
from loguru import logger
from tqdm import tqdm

_VERSION_FILE = ".installed_version"


def _installed_version(models_path: Path) -> str:
    vf = models_path / _VERSION_FILE
    return vf.read_text().strip() if vf.exists() else ""


def _write_version(models_path: Path, version: str) -> None:
    (models_path / _VERSION_FILE).write_text(version)


def download_model(
    models_dir: str = "models",
    gitlab_url: str = "",
    gitlab_project_id: int = 0,
    gitlab_token: str = "",
    model_name: str = "medicinebox",
    model_version: str = "",
) -> None:
    """Download all model files from GitLab if version changed or not yet installed.

    Downloads every file in the package as-is into models_dir, preserving
    the directory structure from the file names. No hardcoded file lists.
    """
    if not gitlab_url or not model_version:
        logger.warning("Skipping model download — gitlab_url or model_version not set")
        return

    models_path = Path(models_dir)
    installed = _installed_version(models_path)
    logger.info(
        f"Installed model version: {installed!r}, Latest version: {model_version!r}"
    )

    if not installed:
        logger.info("No installed model version found — downloading fresh copy")

    if installed == model_version:
        logger.info("Models up-to-date (version={})", model_version)
        return

    logger.info(
        "Model version changed: {} → {} — downloading", installed, model_version
    )

    headers = {"PRIVATE-TOKEN": gitlab_token}
    package_id = _get_package_id(
        gitlab_url, gitlab_project_id, headers, model_name, model_version
    )
    all_files = _get_all_files(gitlab_url, gitlab_project_id, package_id, headers)

    # Download into a staging directory so a failed/partial download never
    # touches the live models_path.  Only swap on full success.
    staging = models_path.parent / (models_path.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        for file_info in all_files:
            file_name = unquote(file_info["file_name"])
            dest = staging / file_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _download_file(
                gitlab_url,
                gitlab_project_id,
                package_id,
                file_info["id"],
                dest,
                headers,
            )
            expected_sha = file_info.get("file_sha256", "")
            if expected_sha and not _verify_sha256(dest, expected_sha):
                raise ValueError(f"SHA256 mismatch: {file_name}")
            logger.info("Downloaded: {}", file_name)

        # All files verified — atomically replace the live models directory.
        if models_path.exists():
            shutil.rmtree(models_path)
        staging.rename(models_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _write_version(models_path, model_version)
    logger.info("Model version {} installed successfully", model_version)


def _get_package_id(
    gitlab_url: str, project_id: int, headers: dict, model_name: str, model_version: str
) -> int:
    url = f"{gitlab_url}/api/v4/projects/{project_id}/packages"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    for pkg in r.json():
        if pkg["name"] == model_name and pkg["version"] == model_version:
            return pkg["id"]
    raise ValueError(f"Package not found: {model_name} {model_version}")


def _get_all_files(
    gitlab_url: str, project_id: int, package_id: int, headers: dict
) -> list[dict]:
    url = (
        f"{gitlab_url}/api/v4/projects/{project_id}/packages/{package_id}/package_files"
    )
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


def _download_file(
    gitlab_url: str,
    project_id: int,
    package_id: int,
    file_id: int,
    dest: Path,
    headers: dict,
) -> None:
    url = (
        f"{gitlab_url}/api/v4/projects/{project_id}/packages/{package_id}"
        f"/package_files/{file_id}/download"
    )
    r = requests.get(url, headers=headers, stream=True)
    r.raise_for_status()
    total_size = int(r.headers.get("content-length", 0))
    with open(dest, "wb") as f, tqdm(
        desc=dest.name,
        total=total_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
    ) as pbar:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                pbar.update(len(chunk))


def _verify_sha256(filepath: Path, expected: str) -> bool:
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest() == expected
