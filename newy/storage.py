from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import Article, Source, User


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()


    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def _executemany(self, sql: str, params: Iterable[tuple[Any, ...]]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, params)

    def _executescript(self, sql: str) -> None:
        with self._lock:
            self.conn.executescript(sql)

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def ensure_schema(self) -> None:
        self._executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                feed_url TEXT NOT NULL,
                base_url TEXT DEFAULT '',
                region TEXT DEFAULT 'global',
                language TEXT DEFAULT 'en',
                trust_tier INTEGER DEFAULT 3,
                source_type TEXT DEFAULT 'rss',
                active INTEGER DEFAULT 1,
                poll_interval_minutes INTEGER DEFAULT 30,
                fallback_allowed INTEGER DEFAULT 0,
                metadata_json TEXT DEFAULT '{}',
                last_polled_at TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_slug TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT UNIQUE NOT NULL,
                author TEXT DEFAULT '',
                published_at TEXT NOT NULL,
                language TEXT DEFAULT 'en',
                summary TEXT DEFAULT '',
                body TEXT DEFAULT '',
                topic_tags_json TEXT DEFAULT '[]',
                region_tags_json TEXT DEFAULT '[]',
                metadata_json TEXT DEFAULT '{}',
                inserted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone_number TEXT UNIQUE NOT NULL,
                preferred_language TEXT DEFAULT 'bilingual',
                topics_json TEXT DEFAULT '[]',
                regions_json TEXT DEFAULT '[]',
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                local_time TEXT NOT NULL,
                timezone TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_sent_on TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                kind TEXT NOT NULL,
                topic TEXT DEFAULT '',
                language TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                citations_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                job_id INTEGER,
                channel TEXT NOT NULL,
                destination TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                external_id TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_slug TEXT NOT NULL,
                status TEXT NOT NULL,
                articles_found INTEGER NOT NULL DEFAULT 0,
                trace_json TEXT NOT NULL DEFAULT '{}',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        self._commit()

    def bootstrap_sources(self, sources: Iterable[Source]) -> None:
        for source in sources:
            self.upsert_source(source)

    def upsert_source(self, source: Source) -> None:
        self._execute(
            """
            INSERT INTO sources (slug, name, feed_url, base_url, region, language, trust_tier, source_type, active, poll_interval_minutes, fallback_allowed, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name,
                feed_url=excluded.feed_url,
                base_url=excluded.base_url,
                region=excluded.region,
                language=excluded.language,
                trust_tier=excluded.trust_tier,
                source_type=excluded.source_type,
                active=excluded.active,
                poll_interval_minutes=excluded.poll_interval_minutes,
                fallback_allowed=excluded.fallback_allowed,
                metadata_json=excluded.metadata_json
            """,
            (
                source.slug,
                source.name,
                source.feed_url,
                source.base_url,
                source.region,
                source.language,
                source.trust_tier,
                source.source_type,
                1 if source.active else 0,
                source.poll_interval_minutes,
                1 if source.fallback_allowed else 0,
                json.dumps(source.metadata),
            ),
        )
        self._commit()

    def list_sources(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sources"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY trust_tier DESC, slug ASC"
        return self._fetchall(sql)

    def get_source_trust_map(self) -> dict[str, int]:
        return {row["slug"]: int(row["trust_tier"]) for row in self.list_sources()}

    def source_due_for_poll(self, row: sqlite3.Row, now: datetime) -> bool:
        last_polled = row["last_polled_at"]
        if not last_polled:
            return True
        elapsed_minutes = (now - datetime.fromisoformat(last_polled)).total_seconds() / 60
        return elapsed_minutes >= int(row["poll_interval_minutes"])

    def mark_source_polled(self, slug: str, error: str = "") -> None:
        self._execute(
            "UPDATE sources SET last_polled_at = ?, last_error = ? WHERE slug = ?",
            (utcnow(), error, slug),
        )
        self._commit()

    def insert_articles(self, articles: Iterable[Article]) -> int:
        inserted = 0
        for article in articles:
            cursor = self._execute(
                """
                INSERT OR IGNORE INTO articles (
                    source_slug, title, url, author, published_at, language, summary, body,
                    topic_tags_json, region_tags_json, metadata_json, inserted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article.source_slug,
                    article.title,
                    article.url,
                    article.author,
                    article.published_at.isoformat(),
                    article.language,
                    article.summary,
                    article.body,
                    json.dumps(article.topic_tags),
                    json.dumps(article.region_tags),
                    json.dumps(article.metadata),
                    utcnow(),
                ),
            )
            inserted += cursor.rowcount
        self._commit()
        return inserted

    def query_articles(self, *, limit: int = 200, hours: int = 48, include_fallback: bool = True) -> list[Article]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        sql = """
            SELECT a.*, s.region, s.fallback_allowed
            FROM articles a
            JOIN sources s ON s.slug = a.source_slug
            WHERE a.published_at >= ?
        """
        params: list[Any] = [cutoff]
        if not include_fallback:
            sql += " AND s.fallback_allowed = 0"
        sql += " ORDER BY a.published_at DESC LIMIT ?"
        params.append(limit)
        rows = self._fetchall(sql, tuple(params))
        return [
            Article(
                source_slug=row["source_slug"],
                title=row["title"],
                url=row["url"],
                author=row["author"],
                published_at=datetime.fromisoformat(row["published_at"]),
                language=row["language"],
                summary=row["summary"],
                body=row["body"],
                topic_tags=json.loads(row["topic_tags_json"]),
                region_tags=json.loads(row["region_tags_json"]) or [row["region"]],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def upsert_user(self, user: User) -> int:
        existing = self._fetchone("SELECT id FROM users WHERE phone_number = ?", (user.phone_number,))
        if existing:
            self._execute(
                """
                UPDATE users
                SET name = ?, preferred_language = ?, topics_json = ?, regions_json = ?, active = ?
                WHERE id = ?
                """,
                (
                    user.name,
                    user.preferred_language,
                    json.dumps(user.topics),
                    json.dumps(user.regions),
                    1 if user.active else 0,
                    existing["id"],
                ),
            )
            self._commit()
            return int(existing["id"])

        cursor = self._execute(
            """
            INSERT INTO users (name, phone_number, preferred_language, topics_json, regions_json, active)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user.name,
                user.phone_number,
                user.preferred_language,
                json.dumps(user.topics),
                json.dumps(user.regions),
                1 if user.active else 0,
            ),
        )
        self._commit()
        return int(cursor.lastrowid)

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM users WHERE id = ?", (user_id,))

    def find_user_by_phone(self, phone_number: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM users WHERE phone_number = ?", (phone_number,))

    def list_users(self) -> list[sqlite3.Row]:
        return self._fetchall("SELECT * FROM users ORDER BY id ASC")

    def upsert_schedule(self, user_id: int, local_time: str, timezone: str, enabled: bool = True) -> None:
        self._execute(
            """
            INSERT INTO schedules (user_id, local_time, timezone, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                local_time = excluded.local_time,
                timezone = excluded.timezone,
                enabled = excluded.enabled
            """,
            (user_id, local_time, timezone, 1 if enabled else 0),
        )
        self._commit()

    def list_schedules(self) -> list[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT schedules.*, users.phone_number, users.preferred_language, users.topics_json, users.regions_json, users.active
            FROM schedules
            JOIN users ON users.id = schedules.user_id
            ORDER BY schedules.id ASC
            """
        )

    def mark_schedule_sent(self, schedule_id: int, local_day: str) -> None:
        self._execute("UPDATE schedules SET last_sent_on = ? WHERE id = ?", (local_day, schedule_id))
        self._commit()

    def enqueue_job(self, kind: str, payload: dict[str, Any], run_at: str | None = None) -> int:
        timestamp = utcnow()
        cursor = self._execute(
            """
            INSERT INTO jobs (kind, payload_json, run_at, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (kind, json.dumps(payload), run_at or timestamp, timestamp, timestamp),
        )
        self._commit()
        return int(cursor.lastrowid)

    def claim_due_jobs(self, limit: int = 10) -> list[sqlite3.Row]:
        rows = self._fetchall(
            """
            SELECT * FROM jobs
            WHERE status = 'pending' AND run_at <= ?
            ORDER BY run_at ASC, id ASC
            LIMIT ?
            """,
            (utcnow(), limit),
        )
        ids = [row["id"] for row in rows]
        if ids:
            self._executemany(
                "UPDATE jobs SET status = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                [("processing", utcnow(), job_id) for job_id in ids],
            )
            self._commit()
        return [self._fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,)) for job_id in ids if self._fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,)) is not None]

    def complete_job(self, job_id: int) -> None:
        self._execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", ("done", utcnow(), job_id))
        self._commit()

    def fail_job(self, job_id: int, error: str, retry_in_seconds: int | None = None) -> None:
        if retry_in_seconds:
            self._execute(
                "UPDATE jobs SET status = ?, last_error = ?, run_at = ?, updated_at = ? WHERE id = ?",
                (
                    "pending",
                    error,
                    (datetime.now(UTC) + timedelta(seconds=retry_in_seconds)).isoformat(),
                    utcnow(),
                    job_id,
                ),
            )
        else:
            self._execute(
                "UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                ("failed", error, utcnow(), job_id),
            )
        self._commit()

    def save_digest(
        self,
        *,
        user_id: int | None,
        kind: str,
        topic: str,
        language: str,
        title: str,
        body: str,
        citations: list[str],
        confidence: float,
    ) -> int:
        cursor = self._execute(
            """
            INSERT INTO digests (user_id, kind, topic, language, title, body, citations_json, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, kind, topic, language, title, body, json.dumps(citations), confidence, utcnow()),
        )
        self._commit()
        return int(cursor.lastrowid)

    def record_delivery(
        self,
        *,
        user_id: int | None,
        job_id: int | None,
        destination: str,
        message: str,
        status: str,
        external_id: str = "",
        error: str = "",
    ) -> None:
        self._execute(
            """
            INSERT INTO deliveries (user_id, job_id, channel, destination, message, status, external_id, error, created_at)
            VALUES (?, ?, 'whatsapp', ?, ?, ?, ?, ?, ?)
            """,
            (user_id, job_id, destination, message, status, external_id, error, utcnow()),
        )
        self._commit()

    def delivery_exists(self, job_id: int) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM deliveries WHERE job_id = ? AND status IN ('sent', 'dry_run') LIMIT 1",
            (job_id,),
        )
        return row is not None

    def list_deliveries(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._fetchall("SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (limit,))

    def record_source_run(
        self,
        *,
        source_slug: str,
        status: str,
        articles_found: int,
        trace: dict[str, Any],
        error: str = "",
    ) -> None:
        self._execute(
            """
            INSERT INTO source_runs (source_slug, status, articles_found, trace_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_slug, status, articles_found, json.dumps(trace), error, utcnow()),
        )
        self._commit()

    def list_source_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._fetchall("SELECT * FROM source_runs ORDER BY id DESC LIMIT ?", (limit,))
