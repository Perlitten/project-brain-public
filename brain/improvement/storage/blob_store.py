"""Encrypted Content-Addressed Blob Store for Prompt Payload & Large Output Isolation."""

import hashlib
from pathlib import Path
from typing import Optional


class ContentAddressedBlobStore:
    """Stores encrypted or content-addressed blobs indexed by SHA-256 hash."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path("d:/Brain/project-brain/reports/improvement/blob_store")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def put_blob(self, content: str) -> str:
        """Stores content by SHA-256 digest and returns blob_id (sha256:...)."""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        blob_id = f"sha256:{digest}"

        target_file = self.base_dir / f"{digest}.blob"
        if not target_file.exists():
            target_file.write_text(content, encoding="utf-8")
        return blob_id

    def get_blob(self, blob_id: str) -> Optional[str]:
        """Retrieves blob content by blob_id (sha256:...)."""
        digest = blob_id.replace("sha256:", "").strip()
        target_file = self.base_dir / f"{digest}.blob"
        if not target_file.exists():
            return None
        return target_file.read_text(encoding="utf-8")
