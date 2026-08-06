"""Prepare the local virtual environment's managed runtime dependencies."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / "runtime"
DEPENDENCY_DIR = RUNTIME_DIR / "deps"
BLINKLIVEVIEW_COMMIT = "d8f0a02180efce003de690055b87e8e2d5482e12"
BLINKLIVEVIEW_SHA256 = "27e5fe91a6f4e0ffe8c55c2b226bda744e1e628fa5810fdc10f87a8ac710a050"
BLINKLIVEVIEW_URL = (
    f"https://github.com/lockieluke/blinkliveview/archive/{BLINKLIVEVIEW_COMMIT}.tar.gz"
)


def digest_files(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def install_python_dependencies() -> None:
    stamp = Path(sys.prefix) / ".blink-dashboard-requirements.sha256"
    wanted = digest_files(PROJECT_ROOT / "pyproject.toml", PROJECT_ROOT / "requirements.lock")
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == wanted:
        return
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(PROJECT_ROOT / "requirements.lock"),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    stamp.write_text(wanted + "\n", encoding="utf-8")


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if destination != target and destination not in target.parents:
            raise RuntimeError(f"unsafe archive path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise RuntimeError(f"unsupported archive entry: {member.name}")
    archive.extractall(destination)


def fetch_blinkliveview() -> Path:
    target = DEPENDENCY_DIR / "blinkliveview"
    manifest = target / ".managed-dependency.json"
    if manifest.exists():
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if (
                metadata["commit"] == BLINKLIVEVIEW_COMMIT
                and metadata["archive_sha256"] == BLINKLIVEVIEW_SHA256
            ):
                return target
        except (OSError, ValueError, KeyError):
            pass

    DEPENDENCY_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blinkliveview-", dir=DEPENDENCY_DIR) as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / "source.tar.gz"
        print("Fetching managed blinkliveview dependency...")
        with urllib.request.urlopen(BLINKLIVEVIEW_URL, timeout=60) as response:
            archive_path.write_bytes(response.read())
        actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if actual != BLINKLIVEVIEW_SHA256:
            raise RuntimeError(
                "blinkliveview archive checksum mismatch; refusing to use unverified source"
            )
        extract_dir = temporary_path / "extract"
        extract_dir.mkdir()
        with tarfile.open(archive_path, mode="r:gz") as archive:
            _safe_extract(archive, extract_dir)
        roots = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("unexpected blinkliveview archive layout")
        staged = temporary_path / "staged"
        shutil.move(str(roots[0]), staged)
        (staged / ".managed-dependency.json").write_text(
            json.dumps(
                {
                    "repository": "https://github.com/lockieluke/blinkliveview",
                    "commit": BLINKLIVEVIEW_COMMIT,
                    "archive_sha256": BLINKLIVEVIEW_SHA256,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target)
        os.replace(staged, target)
    return target


def check_runtime_tools() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg.exe was not found on PATH. Install FFmpeg before launching the dashboard."
        )


def main() -> int:
    try:
        check_runtime_tools()
        install_python_dependencies()
        fetch_blinkliveview()
    except Exception as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    print("Setup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
