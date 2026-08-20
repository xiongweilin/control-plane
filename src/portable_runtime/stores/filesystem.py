from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import unquote, urlparse


class FilesystemArtifactStore:
    """Content-addressed artifact bytes; metadata remains in the state store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, *, media_type: str | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest
        if not path.exists():
            path.write_bytes(data)
        return path.as_uri()

    def get(self, uri: str) -> bytes:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError("artifact URI must use the file scheme")
        raw_path = unquote(parsed.path)
        if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        candidate = Path(raw_path).resolve()
        root = self.root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("invalid artifact URI") from exc
        if candidate == root:
            raise ValueError("invalid artifact URI")
        return candidate.read_bytes()
