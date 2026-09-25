"""Tamper-evident evidence locker.

Each archived clip is written read-only, hashed with SHA-256, and recorded in an
append-only ledger where every entry commits to the previous entry's hash. Editing,
deleting or re-ordering any file or ledger line breaks `verify()`. Hand the ledger
line and the file to police together; they can re-hash independently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path

GENESIS = "0" * 64


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class EvidenceLocker:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = root / "ledger.jsonl"
        self._lock = threading.Lock()

    def _entries(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]

    def store(self, event_id: str, files: dict[str, bytes], meta: dict) -> dict:
        """files: {"clip.mp4": b"...", "snapshot.jpg": b"..."}. Returns the ledger entry."""
        with self._lock:
            entries = self._entries()
            prev = entries[-1]["hash"] if entries else GENESIS
            day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            folder = self.root / day / event_id
            folder.mkdir(parents=True, exist_ok=True)
            hashes: dict[str, str] = {}
            for name, data in files.items():
                path = folder / name
                if path.exists():
                    os.chmod(path, 0o644)
                path.write_bytes(data)
                os.chmod(path, 0o444)
                hashes[str(path.relative_to(self.root))] = sha256(data)
            body = {
                "seq": len(entries),
                "archived_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "event_id": event_id,
                "files": hashes,
                "meta": meta,
                "prev": prev,
            }
            entry = {**body, "hash": sha256(_canonical(body))}
            with self.ledger.open("a") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return entry

    def verify(self) -> tuple[bool, list[str]]:
        problems: list[str] = []
        prev = GENESIS
        for i, entry in enumerate(self._entries()):
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry.get("seq") != i:
                problems.append(f"entry {i}: sequence number is {entry.get('seq')}")
            if body.get("prev") != prev:
                problems.append(f"entry {i}: chain broken (prev hash mismatch)")
            if sha256(_canonical(body)) != entry.get("hash"):
                problems.append(f"entry {i}: entry was modified")
            for rel, digest in body.get("files", {}).items():
                path = self.root / rel
                if not path.exists():
                    problems.append(f"entry {i}: {rel} is missing")
                elif sha256(path.read_bytes()) != digest:
                    problems.append(f"entry {i}: {rel} was modified")
            prev = entry.get("hash", "")
        return not problems, problems
