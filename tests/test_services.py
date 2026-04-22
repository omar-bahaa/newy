import json
import tempfile
import unittest
import unittest.mock
from datetime import UTC, datetime
from pathlib import Path

from newy.config import AppConfig
from newy.models import Article, Source, User
from newy.services import NewyApp


class ServiceTests(unittest.TestCase):
    def test_topic_digest_job_runs_in_dry_run_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seed_path = tmp_path / "sources.json"
            seed_path.write_text("[]")
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "database_path": str(tmp_path / "newy.sqlite3"),
                        "source_seed_path": str(seed_path),
                        "codex": {"enabled": False},
                        "twilio": {"dry_run": True, "from_number": "whatsapp:+14155238886"}
                    }
                )
            )

            app = NewyApp(AppConfig.load(str(config_path)))
            user_id = app.store.upsert_user(
                User(
                    id=None,
                    name="Tester",
                    phone_number="whatsapp:+10000000001",
                    preferred_language="english",
                    topics=["iran"],
                    regions=["middle-east"],
                )
            )
            app.store.upsert_source(Source(slug="seed", name="Seed", feed_url="https://example.com/feed"))
            app.store.insert_articles(
                [
                    Article(
                        source_slug="seed",
                        title="Iran conflict escalates",
                        url="https://example.com/a",
                        summary="Trusted update",
                        body="Trusted update with context about the conflict.",
                        language="en",
                        published_at=datetime.now(UTC),
                        region_tags=["middle-east"],
                    )
                ]
            )
            app.queue_topic_digest(user_id=user_id, topic="iran conflict")
            processed = app.process_jobs(limit=5)
            deliveries = app.store.list_deliveries()
            source_runs = app.store.list_source_runs()
            self.assertEqual(processed, 1)
            self.assertTrue(deliveries)
            self.assertEqual(deliveries[0]["status"], "dry_run")
            self.assertEqual(source_runs, [])

    def test_invalid_timezone_does_not_crash_scheduling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seed_path = tmp_path / "sources.json"
            seed_path.write_text("[]")
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "database_path": str(tmp_path / "newy.sqlite3"),
                        "source_seed_path": str(seed_path),
                        "codex": {"enabled": False}
                    }
                )
            )
            app = NewyApp(AppConfig.load(str(config_path)))
            user_id = app.store.upsert_user(
                User(
                    id=None,
                    name="Tester",
                    phone_number="whatsapp:+10000000002",
                    preferred_language="english",
                )
            )
            app.store.upsert_schedule(user_id, "08:00", "Bad/Timezone", enabled=True)
            queued = app.schedule_due_daily_digests()
            self.assertEqual(queued, 0)


    def test_ingest_records_source_run_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seed_path = tmp_path / "sources.json"
            seed_path.write_text(json.dumps([
                {
                    "slug": "seed",
                    "name": "Seed",
                    "feed_url": "https://example.com/home",
                    "base_url": "https://example.com",
                    "source_type": "archive_page",
                    "language": "en",
                    "region": "global"
                }
            ]))
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({
                "database_path": str(tmp_path / "newy.sqlite3"),
                "source_seed_path": str(seed_path),
                "codex": {"enabled": False},
                "browser": {"enabled": False}
            }))
            app = NewyApp(AppConfig.load(str(config_path)))
            with unittest.mock.patch("newy.services.fetch_source_result") as mocked_fetch:
                from newy.models import SourceFetchResult
                mocked_fetch.return_value = SourceFetchResult(articles=[], status="navigation_exhausted", error="navigation_exhausted", trace={"visited_pages": ["https://example.com/home"]})
                totals = app.ingest_due_sources(force=True)
            self.assertEqual(totals["sources_polled"], 1)
            runs = app.store.list_source_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "navigation_exhausted")
