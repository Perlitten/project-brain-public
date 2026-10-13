"""Small durable TTL cache; Redis clients are never constructed in Lite mode."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from contextlib import contextmanager


class SQLiteCache:
    storage_mode = "lite"

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "cache.sqlite3"
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def _get(self, key):
        with self.connection() as db:
            row = db.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()
        return row[0] if row and (row[1] is None or row[1] > time.time()) else None

    async def get(self, key):
        return await asyncio.to_thread(self._get, key)

    def _set(self, key, value, nx, ex):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM cache WHERE key=? AND expires<=?", (key, time.time()))
            if nx and db.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone():
                return None
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                       (key, str(value), time.time() + float(ex) if ex else None))
        return True

    async def set(self, key, value, *, nx=False, ex=None, **kwargs):
        return await asyncio.to_thread(self._set, key, value, nx, ex)

    async def setex(self, key, seconds, value):
        return await self.set(key, value, ex=seconds)

    async def delete(self, *keys):
        def remove():
            with self.connection() as db:
                count = 0
                for key in keys:
                    count += db.execute("DELETE FROM cache WHERE key=?", (key,)).rowcount
                return count
        return await asyncio.to_thread(remove)

    async def mget(self, keys):
        return [await self.get(key) for key in keys]

    async def expire(self, key, seconds):
        def update():
            with self.connection() as db:
                return db.execute("UPDATE cache SET expires=? WHERE key=?", (time.time() + seconds, key)).rowcount
        return await asyncio.to_thread(update)

    async def hgetall(self, key):
        return json.loads(await self.get(key) or "{}")

    async def hget(self, key, field):
        return (await self.hgetall(key)).get(field)

    async def hset(self, key, mapping: dict[str, Any]):
        def update():
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()
                live = bool(row and (row[1] is None or row[1] > time.time()))
                old = json.loads(row[0]) if live else {}
                old.update(mapping)
                db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(old), row[1] if live else None))
        await asyncio.to_thread(update)

    async def ping(self):
        return True

    async def close(self):
        pass

    async def aclose(self):
        pass
