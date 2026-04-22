from datetime import UTC, datetime, timedelta
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from newy.config import AppConfig, CodexConfig
from newy.models import Article, DigestRequest
from newy.ranking import RankedCluster, cluster_articles, rank_clusters
from newy.summarizer import CodexLocalProvider, DigestEngine, ExtractiveProvider


def _article(source: str, title: str, summary: str, body: str, hours_ago: int, *, url: str | None = None) -> Article:
    return Article(
        source_slug=source,
        title=title,
        url=url or f"https://example.com/{source}/{hours_ago}",
        summary=summary,
        body=body,
        published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        region_tags=["middle-east"],
    )


class DigestTests(unittest.TestCase):
    def test_digest_engine_uses_body_similarity_for_topic_requests(self) -> None:
        config = AppConfig(codex=CodexConfig(enabled=False))
        engine = DigestEngine(config, providers=[ExtractiveProvider(config)])
        request = DigestRequest(kind="topic", language="english", topic="US Iran conflict", regions=["middle-east"])
        articles = [
            _article(
                "source-a",
                "Diplomatic standoff deepens",
                "Fresh threats were exchanged.",
                "Iran and the United States traded new military threats as regional mediation intensified.",
                1,
                url="https://example.com/relevant",
            ),
            _article(
                "source-b",
                "Sports championship ends",
                "A final concluded overnight.",
                "An unrelated sporting event wrapped up after extra time.",
                1,
                url="https://example.com/unrelated",
            ),
        ]
        digest = engine.build_digest(request, articles, {"source-a": 5, "source-b": 4})
        self.assertTrue(digest.bullets)
        self.assertIn("Diplomatic standoff", digest.bullets[0].text)
        self.assertIn("https://example.com/relevant", digest.citations)

    def test_coarse_ranking_keeps_query_as_hint_not_dominant_judgment(self) -> None:
        request = DigestRequest(kind="topic", language="english", topic="Iran ceasefire")
        clusters = cluster_articles(
            [
                _article("trusted-a", "Iran truce monitored", "Summary", "Iran ceasefire monitored by regional observers.", 1),
                _article("trusted-b", "Markets react globally", "Summary", "Global markets reacted to oil uncertainty after the truce.", 1),
            ]
        )
        ranked = rank_clusters(clusters, {"trusted-a": 5, "trusted-b": 5}, request)
        self.assertEqual(len(ranked), 2)
        self.assertGreaterEqual(ranked[0].score, ranked[1].score)
        self.assertTrue(all(item.query_score >= 0 for item in ranked))

    def test_codex_prompt_explicitly_assigns_final_ranking_to_agent(self) -> None:
        config = AppConfig(codex=CodexConfig(enabled=True, command="codex", working_directory="."))
        provider = CodexLocalProvider(config)
        request = DigestRequest(kind="topic", language="bilingual", topic="US Iran conflict")
        clusters = [
            RankedCluster(
                cluster_id="cluster-1",
                score=8.2,
                query_score=4.1,
                articles=[
                    _article(
                        "source-a",
                        "Conflict escalates",
                        "A summary",
                        "Detailed body",
                        1,
                        url="https://example.com/a",
                    )
                ],
            )
        ]
        prompt = provider._build_prompt(request, clusters)
        self.assertIn("final editorial ranker and digest judge", prompt)
        self.assertIn("coarse_rank", prompt)
        self.assertIn("coarse_score", prompt)
        self.assertIn("Treat coarse_rank and coarse_score as shortlist hints only", prompt)

    def test_codex_provider_parses_json_output(self) -> None:
        config = AppConfig(codex=CodexConfig(enabled=True, command="codex", working_directory="."))
        provider = CodexLocalProvider(config)
        request = DigestRequest(kind="topic", language="bilingual", topic="US Iran conflict")
        clusters = [
            RankedCluster(
                cluster_id="cluster-1",
                score=8.2,
                query_score=4.1,
                articles=[
                    _article(
                        "source-a",
                        "Conflict escalates",
                        "A summary",
                        "Detailed body",
                        1,
                        url="https://example.com/a",
                    )
                ],
            )
        ]

        def fake_run(cmd, input, text, capture_output, timeout, cwd):
            output_path = Path(cmd[cmd.index("-o") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "en": {
                            "title": "Topic digest: US Iran conflict",
                            "why": "Grounded summary.",
                            "bullets": [{"text": "Key update", "citations": ["https://example.com/a"]}],
                        },
                        "ar": {
                            "title": "ملخص الموضوع: US Iran conflict",
                            "why": "ملخص موثوق.",
                            "bullets": [{"text": "تحديث رئيسي", "citations": ["https://example.com/a"]}],
                        },
                        "confidence": 0.9,
                    }
                )
            )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("newy.config.shutil.which", return_value="/opt/homebrew/bin/codex"), patch(
            "newy.summarizer.subprocess.run",
            side_effect=fake_run,
        ):
            digest = provider.summarize(request, clusters)
        self.assertEqual(digest.sections["en"]["title"], "Topic digest: US Iran conflict")
        self.assertEqual(digest.sections["ar"]["bullets"][0]["text"], "تحديث رئيسي")
        self.assertEqual(digest.citations, ["https://example.com/a"])
