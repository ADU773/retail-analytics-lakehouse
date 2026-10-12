"""Ingestion manifest: the state table that makes bronze ingestion idempotent.

Every successfully ingested raw file is recorded (path relative to the data dir, SHA-256,
size, row count, run id). Re-running ingestion skips files already present with the same
checksum; a known path whose checksum changed violates the immutable-landing-zone contract
and is reported as an error unless a full refresh is requested.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

MANIFEST_FILE = "_ingest_manifest.jsonl"
STATE_FILE = "_state.json"


@dataclass(frozen=True)
class ManifestEntry:
    """One ingested raw file."""

    source_file: str
    entity: str
    batch_id: str
    sha256: str
    bytes: int
    rows: int
    run_id: str
    ingested_at: str


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


class IngestManifest:
    """Append-only JSON Lines manifest stored next to the bronze tables."""

    def __init__(self, bronze_dir: Path) -> None:
        self.path = bronze_dir / MANIFEST_FILE
        self.entries: dict[str, ManifestEntry] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = ManifestEntry(**json.loads(line))
                    self.entries[entry.source_file] = entry

    def __len__(self) -> int:
        return len(self.entries)

    def status(self, source_file: str, sha256: str) -> str:
        """``new``, ``ingested`` or ``changed`` for a raw file."""
        entry = self.entries.get(source_file)
        if entry is None:
            return "new"
        return "ingested" if entry.sha256 == sha256 else "changed"

    def append(self, new_entries: list[ManifestEntry]) -> None:
        """Persist new entries (one JSON object per line)."""
        if not new_entries:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            for entry in new_entries:
                fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
                self.entries[entry.source_file] = entry
