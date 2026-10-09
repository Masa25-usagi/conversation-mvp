"""Append-only JSONL with a verifiable SHA-256 chain and process ownership."""

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from .contracts import strict_json, utc_now


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def verify(path):
    previous = "0" * 64
    events = []
    path = Path(path)
    if not path.exists():
        return events
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            event = strict_json(line)
            digest = event.get("hash")
            body = {key: value for key, value in event.items() if key != "hash"}
            if event.get("sequence") != index or event.get("previous_hash") != previous:
                raise ValueError(f"audit chain mismatch at line {index}")
            if hashlib.sha256(canonical(body).encode()).hexdigest() != digest:
                raise ValueError(f"audit hash mismatch at line {index}")
            previous = digest
            events.append(event)
    return events


class Audit:
    def __init__(self, runtime):
        self.root = Path(runtime)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "audit.jsonl"
        # POSIX is the supported MVP platform (macOS / Linux).
        import fcntl
        self._ownership = (self.root / ".runtime.lock").open("a")
        try:
            fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._ownership.close()
            raise ValueError("this runtime is already in use") from None
        try:
            self.events = verify(self.path)
        except BaseException:
            self._ownership.close()
            raise
        self.previous = self.events[-1]["hash"] if self.events else "0" * 64
        self.lock = threading.RLock()
        self.session_id = uuid.uuid4().hex

    def append(self, kind, **data):
        with self.lock:
            event = {"sequence": len(self.events) + 1, "event_id": uuid.uuid4().hex,
                     "session_id": self.session_id, "logged_at": utc_now(), "kind": kind,
                     "previous_hash": self.previous, "data": data}
            event["hash"] = hashlib.sha256(canonical(event).encode()).hexdigest()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                payload = (canonical(event) + "\n").encode()
                with os.fdopen(fd, "ab") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                raise
            self.events.append(event)
            self.previous = event["hash"]
            return event

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def close(self):
        self._ownership.close()
