"""SQLite 追加式审计账本；缓存及队列状态可更新，正式快照不可改写。"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def pack(value):
    return b"GZ1" + gzip.compress(encode(value).encode("utf-8"), mtime=0)


def unpack(value):
    if isinstance(value, bytes) and value.startswith(b"GZ1"):
        return json.loads(gzip.decompress(value[3:]))
    return json.loads(value)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "value.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, as_of TEXT NOT NULL,
          kind TEXT NOT NULL, scope_key TEXT NOT NULL, rule_hash TEXT NOT NULL,
          rules_json TEXT NOT NULL, coverage_json TEXT NOT NULL, results_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations (
          snapshot_id TEXT NOT NULL REFERENCES snapshots(id), code TEXT NOT NULL,
          content_hash TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(snapshot_id,code));
        CREATE TABLE IF NOT EXISTS ai_tasks (
          id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), code TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt TEXT NOT NULL, last_error TEXT, UNIQUE(snapshot_id,code));
        CREATE TABLE IF NOT EXISTS ai_analyses (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES ai_tasks(id), created_at TEXT NOT NULL,
          status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS schedule_slots (
          slot TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id));
        CREATE TABLE IF NOT EXISTS cache_entries (
          cache_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        for table in ("snapshots", "observations", "ai_analyses", "schedule_slots"):
            for action in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_blocked BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'正式记录只允许插入'); END")
        self.db.commit()

    def close(self):
        self.db.close()

    def cached(self, key, max_age_seconds):
        row = self.db.execute("SELECT * FROM cache_entries WHERE cache_key=?", (key,)).fetchone()
        if row and (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds() < max_age_seconds:
            return unpack(row["payload"])
        return None

    def cache(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO cache_entries VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET created_at=excluded.created_at,payload=excluded.payload", (key, utc_now(), pack(value)))

    def save(self, observations, results, rules, coverage, *, kind="live", scope_key="all", slot=None):
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
        with self.db:
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)", (identifier, utc_now(), coverage.get("as_of", ""), kind, scope_key, digest(rules), encode(rules), encode(coverage), encode(results)))
            self.db.executemany("INSERT INTO observations VALUES(?,?,?,?)", [(identifier, o.stock.code, digest(o.to_dict()), pack(o.to_dict())) for o in observations])
            for item in results:
                if kind == "live" and item["status"] == "候选":
                    self.db.execute("INSERT INTO ai_tasks(id,snapshot_id,code,next_attempt) VALUES(?,?,?,?)", (uuid4().hex, identifier, item["code"], utc_now()))
            if slot:
                self.db.execute("INSERT INTO schedule_slots VALUES(?,?)", (slot, identifier))
        return identifier

    def observation(self, identifier, code):
        row = self.db.execute("SELECT content_hash,payload FROM observations WHERE snapshot_id=? AND code=?", (identifier, code)).fetchone()
        if not row:
            raise ValueError("快照没有该公司观测")
        value = unpack(row["payload"])
        if digest(value) != row["content_hash"]:
            raise ValueError("快照观测完整性校验失败")
        return value

    def get(self, identifier=None):
        row = self.db.execute("SELECT * FROM snapshots WHERE id=?", (identifier,)).fetchone() if identifier else self.db.execute("SELECT * FROM snapshots ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        if not row:
            raise ValueError("没有可用快照")
        return {**dict(row), "rules": json.loads(row["rules_json"]), "coverage": json.loads(row["coverage_json"]), "results": json.loads(row["results_json"])}

    def changes(self, snapshot):
        prev = self.db.execute("SELECT id,results_json,coverage_json FROM snapshots WHERE rowid < (SELECT rowid FROM snapshots WHERE id=?) AND kind=? AND scope_key=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (snapshot["id"], snapshot["kind"], snapshot["scope_key"])).fetchone()
        current = {r["code"] for r in snapshot["results"] if r["status"] == "候选"}
        if not prev:
            return {"previous": None, "added": sorted(current), "removed": [], "retained": [], "note": "首份同范围名单，无可比较前次"}
        older = {r["code"] for r in json.loads(prev["results_json"]) if r["status"] == "候选"}
        return {"previous": prev["id"], "added": sorted(current-older), "removed": sorted(older-current), "retained": sorted(current & older), "note": "退出可能源于数据缺失或规则变化，需结合本次原因；不是卖出信号"}


@contextmanager
def process_lock(root):
    """OS 文件锁自动随进程释放，无须猜测或删除所谓过期锁。"""
    path = Path(root).resolve()
    path.mkdir(parents=True, exist_ok=True)
    with (path / "run.lock").open("a+b") as stream:
        try:
            import os
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有扫描或 AI 补做正在运行，请稍后重试") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
