from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from .config import AppConfig
from .delivery import WhatsAppDelivery
from .feed_fetcher import fetch_source_result
from .models import DigestRequest, Source, User
from .source_catalog import load_source_seed
from .storage import Store
from .summarizer import DigestEngine


class NewyApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = Store(config.resolve_path(config.database_path))
        seed_path = config.resolve_path(config.source_seed_path)
        if seed_path.exists():
            self.store.bootstrap_sources(load_source_seed(seed_path))
        self.delivery = WhatsAppDelivery(config)
        self.digest_engine = DigestEngine(config)

    def ingest_due_sources(self, force: bool = False) -> dict[str, int]:
        totals = {"sources_polled": 0, "articles_inserted": 0}
        now = datetime.now(UTC)
        for row in self.store.list_sources(active_only=True):
            if not force and not self.store.source_due_for_poll(row, now):
                continue
            try:
                source = Source(
                    slug=row["slug"],
                    name=row["name"],
                    feed_url=row["feed_url"],
                    base_url=row["base_url"],
                    region=row["region"],
                    language=row["language"],
                    trust_tier=int(row["trust_tier"]),
                    source_type=row["source_type"],
                    active=bool(row["active"]),
                    poll_interval_minutes=int(row["poll_interval_minutes"]),
                    fallback_allowed=bool(row["fallback_allowed"]),
                    metadata=json.loads(row["metadata_json"]),
                )
                fetch_result = fetch_source_result(source, self.config)
                inserted = self.store.insert_articles(fetch_result.articles)
                error = fetch_result.error
                self.store.mark_source_polled(source.slug, error)
                self.store.record_source_run(
                    source_slug=source.slug,
                    status=fetch_result.status,
                    articles_found=inserted,
                    trace=fetch_result.trace,
                    error=error,
                )
                totals["sources_polled"] += 1
                totals["articles_inserted"] += inserted
            except Exception as exc:
                self.store.mark_source_polled(row["slug"], str(exc))
                self.store.record_source_run(
                    source_slug=row["slug"],
                    status="failed",
                    articles_found=0,
                    trace={"error": str(exc)},
                    error=str(exc),
                )
        return totals

    def schedule_due_daily_digests(self) -> int:
        queued = 0
        now_utc = datetime.now(UTC)
        for row in self.store.list_schedules():
            if not row["enabled"] or not row["active"]:
                continue
            try:
                local_now = now_utc.astimezone(ZoneInfo(row["timezone"]))
            except ZoneInfoNotFoundError:
                continue
            hour, minute = [int(part) for part in row["local_time"].split(":", 1)]
            local_day = local_now.date().isoformat()
            if row["last_sent_on"] == local_day:
                continue
            if (local_now.hour, local_now.minute) < (hour, minute):
                continue
            payload = {
                "user_id": row["user_id"],
                "language": row["preferred_language"],
                "regions": json.loads(row["regions_json"]),
                "user_topics": json.loads(row["topics_json"]),
            }
            self.store.enqueue_job("daily_digest", payload)
            self.store.mark_schedule_sent(row["id"], local_day)
            queued += 1
        return queued

    def queue_topic_digest(self, *, user_id: int, topic: str, language: str | None = None) -> int:
        user = self.store.get_user(user_id)
        if not user:
            raise ValueError(f"Unknown user {user_id}")
        payload = {
            "user_id": user_id,
            "topic": topic,
            "language": language or user["preferred_language"],
            "regions": json.loads(user["regions_json"]),
            "user_topics": json.loads(user["topics_json"]),
        }
        return self.store.enqueue_job("topic_digest", payload)

    def process_jobs(self, limit: int = 10) -> int:
        processed = 0
        for job in self.store.claim_due_jobs(limit=limit):
            try:
                payload = json.loads(job["payload_json"])
                if job["kind"] == "daily_digest":
                    self._handle_digest_job(job["id"], "daily", payload)
                elif job["kind"] == "topic_digest":
                    self._handle_digest_job(job["id"], "topic", payload)
                else:
                    raise ValueError(f"Unsupported job kind: {job['kind']}")
                self.store.complete_job(job["id"])
                processed += 1
            except Exception as exc:
                self.store.fail_job(job["id"], str(exc), retry_in_seconds=60)
        return processed

    def _handle_digest_job(self, job_id: int, kind: str, payload: dict) -> None:
        if self.store.delivery_exists(job_id):
            return
        user = self.store.get_user(int(payload["user_id"]))
        if not user:
            raise ValueError("User missing for digest job")

        request = DigestRequest(
            kind=kind,
            language=payload.get("language", user["preferred_language"]),
            user_id=user["id"],
            topic=payload.get("topic", ""),
            regions=payload.get("regions", json.loads(user["regions_json"])),
            user_topics=payload.get("user_topics", json.loads(user["topics_json"])),
            max_items=self.config.max_articles_per_digest,
        )
        primary_articles = self.store.query_articles(hours=self.config.max_article_age_hours, include_fallback=False)
        digest = self.digest_engine.build_digest(request, primary_articles, self.store.get_source_trust_map())
        if kind == "topic" and (not digest.citations or digest.confidence < 0.35):
            digest = self.digest_engine.build_digest(
                request,
                self.store.query_articles(hours=self.config.max_article_age_hours, include_fallback=True),
                self.store.get_source_trust_map(),
            )
        message = self.delivery.render_message(digest, request.language)
        result = self.delivery.send(user["phone_number"], digest, request.language)
        digest_id = self.store.save_digest(
            user_id=user["id"],
            kind=kind,
            topic=request.topic,
            language=request.language,
            title=digest.title,
            body=message,
            citations=digest.citations,
            confidence=digest.confidence,
        )
        self.store.record_delivery(
            user_id=user["id"],
            job_id=job_id,
            destination=user["phone_number"],
            message=message,
            status=result.status,
            external_id=result.external_id or str(digest_id),
            error=result.error,
        )
        if not result.ok:
            raise RuntimeError(result.error or "delivery failed")

    def run_worker(self, once: bool = False) -> None:
        while True:
            self.ingest_due_sources()
            self.schedule_due_daily_digests()
            self.process_jobs()
            if once:
                return
            time.sleep(self.config.worker_poll_interval_seconds)

    def seed_demo_user(self) -> int:
        user_id = self.store.upsert_user(
            User(
                id=None,
                name="Demo User",
                phone_number="whatsapp:+10000000000",
                preferred_language="bilingual",
                topics=["world", "middle east"],
                regions=["global", "middle-east"],
            )
        )
        self.store.upsert_schedule(user_id, "08:00", self.config.timezone_default, enabled=True)
        return user_id
