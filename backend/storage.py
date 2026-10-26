"""Image storage adapter. Local files for development; S3 is the later cloud adapter (same interface).

The database only stores storage keys, checksums and provenance, never
presigned URLs. Keys are validated so a key can never escape the root.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,200}$")


class StorageError(RuntimeError):
    pass


class LocalImageStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not KEY.match(key) or ".." in key.split("/"):
            raise StorageError("invalid storage key")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError("invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)  # atomic: readers never see half a file
        return key

    def get(self, key: str) -> Optional[bytes]:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()

    def copy(self, source: str, target: str) -> str:
        data = self.get(source)
        if data is None:
            raise StorageError("source image missing")
        return self.put(target, data)
