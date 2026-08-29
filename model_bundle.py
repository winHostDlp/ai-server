"""Deterministic identity for one deployed model directory."""

from __future__ import annotations

import hashlib
from pathlib import Path


class ModelBundleError(RuntimeError):
    """The model directory cannot be represented as a safe regular-file bundle."""


def model_bundle_sha256(path: str | Path) -> str:
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ModelBundleError("모델 번들 경로는 symlink가 아닌 디렉터리여야 합니다.")

    files: list[tuple[str, Path]] = []
    try:
        entries = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
        for item in entries:
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                raise ModelBundleError(f"모델 번들에 symlink가 포함될 수 없습니다: {relative}")
            if item.is_dir():
                continue
            if not item.is_file():
                raise ModelBundleError(
                    f"모델 번들에 특수 파일이 포함될 수 없습니다: {relative}"
                )
            files.append((relative, item))
    except OSError as exc:
        raise ModelBundleError("모델 번들 디렉터리를 읽을 수 없습니다.") from exc

    if not files:
        raise ModelBundleError("모델 번들 디렉터리가 비어 있습니다.")

    digest = hashlib.sha256()
    try:
        for relative, file_path in files:
            relative_bytes = relative.encode("utf-8")
            file_size = file_path.stat().st_size
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(file_size.to_bytes(8, "big"))
            bytes_read = 0
            with file_path.open("rb") as file_obj:
                for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                    bytes_read += len(chunk)
                    digest.update(chunk)
            if bytes_read != file_size:
                raise ModelBundleError(
                    f"모델 번들 파일이 해시 중 변경되었습니다: {relative}"
                )
    except (OSError, UnicodeError, OverflowError) as exc:
        raise ModelBundleError("모델 번들 해시를 계산할 수 없습니다.") from exc
    return digest.hexdigest()
