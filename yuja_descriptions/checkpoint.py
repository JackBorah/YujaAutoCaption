"""Durable write-ahead results: never repeat a possibly submitted AI request."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def atomic_json(path: Path, value: dict) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Checkpoint:
    def __init__(self, path: Path, digest: str, records: dict[str, str] | None = None,
                 legacy_records: dict[str, str] | None = None):
        self.path = path
        self.data = {"version": 1, "source_sha256": digest, "results": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if self.data.get("version") != 1:
                raise ValueError('Unsupported checkpoint version')
            if records is None:
                if self.data.get('source_sha256') != digest:
                    raise ValueError('Checkpoint belongs to a different input workbook. Use a different output path.')
            else:
                known = self.data.get('records')
                if known is None and self.data.get('source_sha256') == digest:
                    known = records
                if known is None:
                    known = legacy_records
                if known is None:
                    raise ValueError('Cannot migrate this older checkpoint: its processed workbook is missing. '
                                     'Restore that output workbook alongside the checkpoint.')
                completed = set(self.data['results'])
                overlap = completed.intersection(records)
                if completed and not overlap and self.data.get('source_sha256') != digest and self.data.get('workflow') not in {'users', 'spreadsheet'}:
                    raise ValueError('No checkpointed videos match this input. Choose a separate --output for a different batch.')
                for key in overlap:
                    if known.get(key) != records[key]:
                        raise ValueError(f'Title or owner changed for previously checked {key}. '
                                         'Review that record before resuming; the previous request has been preserved.')
                self.data['records'] = {**known, **records}
        if records is not None:
            self.data.setdefault('records', records)
            self.data['source_sha256'] = digest

    def save(self):
        atomic_json(self.path, self.data)

    def get(self, key: str):
        return self.data["results"].get(key)

    def put(self, key: str, status: str, note: str) -> dict:
        result = {"status": status, "note": note, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.data["results"][key] = result
        atomic_json(self.path, self.data)
        return result


class RunLock:
    """Avoid two processes processing the same workbook simultaneously."""
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(f"Run lock exists: {self.path}. If the prior process ended, remove this lock and retry.") from None
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        self.path.unlink(missing_ok=True)
