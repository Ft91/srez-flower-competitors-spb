"""Persistent local history. SQLite commits and append-only artifact folders."""
from contextlib import closing, contextmanager
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class HistoryStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.db = self.root / "history.sqlite3"
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                competitor_id TEXT, parent_id TEXT, status TEXT NOT NULL, body TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS history_created ON history(created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS history_competitor ON history(competitor_id)")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.db, timeout=15)) as connection:
            with connection:
                yield connection

    def new(self, kind, *, competitor_id=None, parent_id=None, source=None):
        return {
            "id": uuid4().hex, "created_at": utc_now(), "finished_at": None,
            "kind": kind, "competitor_id": competitor_id, "parent_id": parent_id,
            "status": "running", "source": source, "artifacts": {},
            "result": None, "error": None,
        }

    def save(self, record):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO history VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, body=excluded.body",
                (record["id"], record["created_at"], record["kind"], record["competitor_id"],
                 record["parent_id"], record["status"], json.dumps(record, ensure_ascii=False)),
            )
        return record

    def finish(self, record, *, result=None, error=None):
        record.update(status="failed" if error else "completed", finished_at=utc_now(),
                      result=result, error=error)
        return self.save(record)

    def put_file(self, record, name, data: bytes, media_type):
        # All filenames are internal constants; no user filename becomes a path.
        if name not in {"page.txt", "page.json", "viewport.png", "full-page.png", "input.txt", "input.png", "input.jpg"}:
            raise ValueError("Unknown artifact")
        directory = self.artifacts / record["id"]
        directory.mkdir(exist_ok=True)
        path = directory / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)
        record["artifacts"][name] = {
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "media_type": media_type, "url": f"/history/{record['id']}/files/{name}",
        }

    def get(self, record_id):
        with self.connect() as connection:
            row = connection.execute("SELECT body FROM history WHERE id=?", (record_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, limit=20, offset=0, *, competitor_id=None, kind=None):
        clauses, values = [], []
        for key, value in [("competitor_id", competitor_id), ("kind", kind)]:
            if value is not None:
                clauses.append(key + "=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM history" + where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT body FROM history" + where + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        items = []
        for row in rows:
            item = json.loads(row[0])
            item.pop("result")
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def file(self, record_id, name):
        record = self.get(record_id)
        if not record or name not in record["artifacts"]:
            return None
        path = self.artifacts / record_id / name
        if not path.is_file() or not path.resolve().is_relative_to(self.artifacts):
            return None
        return path, record["artifacts"][name]["media_type"]
