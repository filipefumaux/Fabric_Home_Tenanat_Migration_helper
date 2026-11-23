import importlib.metadata
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

_SEED_DELTA_VERSION = "0.22.3"
_SEED_RUNTIME_TEMP = None
_SEED_DELTA_WHEELS = {
    "linux": (
        "https://files.pythonhosted.org/packages/7c/09/95abe066d746fb3be6c74dce78f3c847edf8b274062fb73b0b5afdae3d8f/"
        "deltalake-0.22.3-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "636c657ac0a6f439c4f6a180f43f4d521e91ced8a8041cb69001de0e566518b9"),
    "win32": (
        "https://files.pythonhosted.org/packages/f6/c4/10f33c1b6aee253fe88ba4ad02d7ab1532b2de9f91a9df5b1e435e9fabdb/"
        "deltalake-0.22.3-cp39-abi3-win_amd64.whl",
        "65d9b477ff6e7ae2333da86d268551b3c7ff6654ba65f1c4900670f8ad6fba2b"),
}


def _seed_download_wheel(url, expected_sha):
    digest, data = hashlib.sha256(), io.BytesIO()
    # Public package download: never send notebook credentials to the package host.
    resp = requests.get(url, stream=True, timeout=300)
    try:
        resp.raise_for_status()
        for chunk in resp.iter_content(1024 * 1024):
            if data.tell() + len(chunk) > _SEED_MAX_FILE_BYTES:
                raise SeedError("Pinned Delta wheel exceeds the 64 MiB download bound.")
            digest.update(chunk)
            data.write(chunk)
    finally:
        resp.close()
    if digest.hexdigest() != expected_sha:
        raise SeedError("Pinned Delta wheel SHA-256 mismatch. No package was installed or imported.")
    return data.getvalue()


def seed_prepare_runtime():
    global _SEED_RUNTIME_TEMP
    if _SEED_RUNTIME_TEMP is not None:
        return
    if "deltalake" in sys.modules:
        raise SeedError("The kernel already imported deltalake. Start a fresh Python job for the isolated stress "
                        "runtime; no loaded native module will be replaced.")
    if sys.platform not in _SEED_DELTA_WHEELS or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SeedError("The pinned stress runtime supports Linux/Windows x86-64 only; no unverified wheel was used.")
    try:
        arrow_version = importlib.metadata.version("pyarrow")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SeedError("The isolated Delta runtime requires existing PyArrow 16+. No shared environment was changed.") from exc
    if int(arrow_version.split(".", 1)[0]) < 16:
        raise SeedError(f"The isolated Delta runtime requires existing PyArrow 16+, found {arrow_version}. "
                        "No loaded/shared PyArrow package was changed.")
    url, expected_sha = _SEED_DELTA_WHEELS[sys.platform]
    data = _seed_download_wheel(url, expected_sha)
    _SEED_RUNTIME_TEMP = tempfile.TemporaryDirectory(prefix="v2t-stress-delta-")
    root = Path(_SEED_RUNTIME_TEMP.name)
    wheel = root / url.rsplit("/", 1)[-1]
    wheel.write_bytes(data)
    target = root / "packages"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(target), str(wheel)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    if result.returncode:
        raise SeedError(f"Job-local Delta installation failed (exit {result.returncode}):\n{result.stdout}")
    sys.path.insert(0, str(target))
    import deltalake
    if deltalake.__version__ != _SEED_DELTA_VERSION:
        raise SeedError(f"Wrong isolated Delta version: {deltalake.__version__}; expected {_SEED_DELTA_VERSION}.")
    print(f"Stress-only runtime: deltalake {_SEED_DELTA_VERSION}, SHA-256 verified, existing PyArrow {arrow_version}; "
          "temporary job directory only; no shared environment change.")


if ISOLATED_STRESS_RUNTIME and LARGE_DATA_MIB:
    seed_prepare_runtime()
