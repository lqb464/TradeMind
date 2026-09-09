"""Atomic model-artifact persistence with human-readable manifests and SHA256."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def artifact_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_path(path: str | Path) -> Path:
    artifact = Path(path)
    return artifact.with_suffix(artifact.suffix + ".manifest.json")


def checksum_path(path: str | Path) -> Path:
    artifact = Path(path)
    return artifact.with_suffix(artifact.suffix + ".sha256")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_joblib_artifact(
    artifact: Mapping[str, Any],
    path: str | Path,
    *,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically save a joblib bundle and adjacent JSON/checksum sidecars."""

    try:
        import joblib
    except ImportError as exc:  # pragma: no cover - project install includes joblib
        raise RuntimeError("joblib is required to save model artifacts") from exc

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        joblib.dump(dict(artifact), temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    checksum = artifact_sha256(destination)
    manifest_payload = {
        **dict(manifest),
        "artifact_file": destination.name,
        "artifact_sha256": checksum,
    }
    _atomic_text(
        manifest_path(destination),
        json.dumps(
            manifest_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
    )
    _atomic_text(checksum_path(destination), f"{checksum}  {destination.name}\n")
    return manifest_payload


def verify_artifact(path: str | Path) -> dict[str, Any]:
    """Verify an artifact against both sidecars and return its manifest."""

    artifact = Path(path)
    manifest_file = manifest_path(artifact)
    checksum_file = checksum_path(artifact)
    if (
        not artifact.is_file()
        or not manifest_file.is_file()
        or not checksum_file.is_file()
    ):
        raise FileNotFoundError("artifact, manifest and SHA256 sidecar must all exist")
    manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    expected_manifest = str(manifest_payload.get("artifact_sha256") or "")
    expected_sidecar = checksum_file.read_text(encoding="utf-8").split()[0]
    actual = artifact_sha256(artifact)
    if (
        not expected_manifest
        or actual != expected_manifest
        or actual != expected_sidecar
    ):
        raise ValueError("model artifact SHA256 verification failed")
    return manifest_payload


def load_joblib_artifact(path: str | Path, *, verify: bool = True) -> dict[str, Any]:
    """Load only after optional checksum verification.

    Joblib uses pickle internally and must therefore only be used with artifacts
    produced by this trusted local pipeline.  SHA256 detects corruption; it is
    not a substitute for a signed supply chain.
    """

    if verify:
        verify_artifact(path)
    try:
        import joblib
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("joblib is required to load model artifacts") from exc
    loaded = joblib.load(Path(path))
    if not isinstance(loaded, dict):
        raise ValueError("model artifact must contain a dictionary bundle")
    return loaded
