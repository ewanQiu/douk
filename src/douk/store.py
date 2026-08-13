"""SQLite 存储层：aweme_id 主键天然去重，status 驱动断点续传。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS target (
  target_id   TEXT PRIMARY KEY,          -- dramaId / mixId / secUid / collectionId
  kind        TEXT NOT NULL,             -- drama | mix | user | collection
  name        TEXT,
  url         TEXT,
  author_uid  TEXT,
  author_name TEXT,
  video_count INTEGER DEFAULT 0,
  description TEXT,                      -- 简介
  cover_url   TEXT,                      -- 封面原始链接（签名会过期，及时下载）
  cover_path  TEXT,                      -- 封面落地后的本地路径
  themes      TEXT,                      -- 题材标签，逗号分隔
  total_duration INTEGER,                -- 全剧总时长（秒）
  num_watched INTEGER,                   -- 观看数
  created_at  INTEGER,
  updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS aweme (
  aweme_id    TEXT PRIMARY KEY,
  target_id   TEXT NOT NULL,
  seq         INTEGER,                   -- 正序集数（1 = 最早）
  title       TEXT,
  create_time INTEGER,
  duration    INTEGER,
  author_name TEXT,
  cover_url   TEXT,
  web_url     TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed|skipped
  file_path   TEXT,
  file_size   INTEGER,
  retry       INTEGER DEFAULT 0,
  error       TEXT,
  added_at    INTEGER,
  updated_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_aweme_target ON aweme(target_id, seq);
CREATE INDEX IF NOT EXISTS idx_aweme_status ON aweme(status);
"""


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """老库补列。SQLite 的 ADD COLUMN 没有 IF NOT EXISTS，只能先查再加。"""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(target)")}
        for col, ddl in (
            ("description", "TEXT"), ("cover_url", "TEXT"), ("cover_path", "TEXT"),
            ("themes", "TEXT"), ("total_duration", "INTEGER"),
            ("num_watched", "INTEGER"),
        ):
            if col not in have:
                self.db.execute(f"ALTER TABLE target ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        self.db.close()

    # ---------- target ----------

    def upsert_target(self, target_id: str, kind: str, **kw: Any) -> None:
        cols = {"name", "url", "author_uid", "author_name", "video_count",
                "description", "cover_url", "cover_path", "themes",
                "total_duration", "num_watched"}
        kw = {k: v for k, v in kw.items() if k in cols and v is not None}
        self.db.execute(
            "INSERT INTO target(target_id, kind, created_at, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(target_id) DO NOTHING",
            (target_id, kind, now(), now()),
        )
        if kw:
            sets = ", ".join(f"{k}=?" for k in kw)
            self.db.execute(
                f"UPDATE target SET {sets}, updated_at=? WHERE target_id=?",
                (*kw.values(), now(), target_id),
            )
        self.db.commit()

    def get_target(self, target_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM target WHERE target_id=?", (target_id,)
        ).fetchone()

    def list_targets(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM aweme a WHERE a.target_id=t.target_id) AS known, "
            "(SELECT COUNT(*) FROM aweme a WHERE a.target_id=t.target_id AND a.status='done') AS done "
            "FROM target t ORDER BY updated_at DESC"
        ).fetchall()

    # ---------- aweme ----------

    def add_awemes(self, target_id: str, items: Iterable[dict]) -> int:
        """插入新视频，返回本次新增条数。已存在的不动（保住 status/file_path）。"""
        added = 0
        for it in items:
            cur = self.db.execute(
                "INSERT INTO aweme(aweme_id, target_id, seq, title, create_time, "
                "duration, author_name, cover_url, web_url, added_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(aweme_id) DO NOTHING",
                (
                    it["aweme_id"], target_id, it.get("seq"), it.get("title"),
                    it.get("create_time"), it.get("duration"), it.get("author_name"),
                    it.get("cover_url"), it.get("web_url"), now(), now(),
                ),
            )
            added += cur.rowcount
        self.db.commit()
        return added

    def known_ids(self, target_id: str) -> set[str]:
        rows = self.db.execute(
            "SELECT aweme_id FROM aweme WHERE target_id=?", (target_id,)
        ).fetchall()
        return {r["aweme_id"] for r in rows}

    def renumber(self, target_id: str) -> None:
        """按 create_time 正序重排 seq —— TikTok 返回的是新→旧，剧集要正序才对。"""
        rows = self.db.execute(
            "SELECT aweme_id FROM aweme WHERE target_id=? "
            "ORDER BY COALESCE(create_time, 0) ASC, aweme_id ASC",
            (target_id,),
        ).fetchall()
        for i, r in enumerate(rows, start=1):
            self.db.execute(
                "UPDATE aweme SET seq=? WHERE aweme_id=?", (i, r["aweme_id"])
            )
        self.db.commit()

    def pending(self, target_id: str | None, max_retry: int) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM aweme WHERE status IN ('pending','failed') AND retry < ? ")
        args: list[Any] = [max_retry]
        if target_id:
            sql += "AND target_id=? "
            args.append(target_id)
        sql += "ORDER BY seq ASC, aweme_id ASC"
        return self.db.execute(sql, args).fetchall()

    def mark_done(self, aweme_id: str, file_path: str, size: int) -> None:
        self.db.execute(
            "UPDATE aweme SET status='done', file_path=?, file_size=?, error=NULL, "
            "updated_at=? WHERE aweme_id=?",
            (file_path, size, now(), aweme_id),
        )
        self.db.commit()

    def mark_failed(self, aweme_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE aweme SET status='failed', retry=retry+1, error=?, updated_at=? "
            "WHERE aweme_id=?",
            (error[:2000], now(), aweme_id),
        )
        self.db.commit()

    def failed_rows(self, target_id: str | None, max_retry: int,
                    stuck_only: bool) -> list[sqlite3.Row]:
        """失败条目。stuck_only=True 时只列已达重试上限、download 不会再碰的那些。"""
        sql = "SELECT * FROM aweme WHERE status='failed' "
        args: list[Any] = []
        if stuck_only:
            sql += "AND retry >= ? "
            args.append(max_retry)
        if target_id:
            sql += "AND target_id=? "
            args.append(target_id)
        return self.db.execute(sql + "ORDER BY seq", args).fetchall()

    def reset_retry(self, target_id: str | None, max_retry: int,
                    stuck_only: bool) -> int:
        """把失败条目退回 pending 并清零重试计数，让 download 重新捡起它们。"""
        sql = ("UPDATE aweme SET status='pending', retry=0, error=NULL, updated_at=? "
               "WHERE status='failed' ")
        args: list[Any] = [now()]
        if stuck_only:
            sql += "AND retry >= ? "
            args.append(max_retry)
        if target_id:
            sql += "AND target_id=? "
            args.append(target_id)
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.rowcount

    def stats(self, target_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) c FROM aweme "
        args: list[Any] = []
        if target_id:
            sql += "WHERE target_id=? "
            args.append(target_id)
        sql += "GROUP BY status"
        return {r["status"]: r["c"] for r in self.db.execute(sql, args).fetchall()}
