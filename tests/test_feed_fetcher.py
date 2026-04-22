from datetime import UTC
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from newy.config import AppConfig, BrowserConfig, CodexConfig
from newy.feed_fetcher import fetch_archive_page_result, parse_archive_page_bytes, parse_article_page_bytes, parse_rss_bytes
from newy.models import Source


class FeedFetcherTests(unittest.TestCase):
    def test_parse_rss_bytes_extracts_items(self) -> None:
        source = Source(slug="bbc", name="BBC", feed_url="http://example.com/feed.xml")
        xml = b"""
        <rss version="2.0">
          <channel>
            <item>
              <title>Headline One</title>
              <link>https://example.com/a</link>
              <description>Summary A</description>
              <pubDate>Wed, 09 Apr 2026 10:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """
        articles = parse_rss_bytes(xml, source)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Headline One")
        self.assertEqual(articles[0].url, "https://example.com/a")
        self.assertEqual(articles[0].published_at.tzinfo, UTC)

    def test_parse_rss_bytes_rejects_unsafe_entity(self) -> None:
        source = Source(slug="bbc", name="BBC", feed_url="http://example.com/feed.xml")
        xml = b'<!DOCTYPE foo [ <!ENTITY xxe "boom"> ]><rss version="2.0"><channel></channel></rss>'
        with self.assertRaises(ValueError):
            parse_rss_bytes(xml, source)

    def test_parse_archive_page_extracts_article_links(self) -> None:
        source = Source(
            slug="archive",
            name="Archive",
            feed_url="https://example.com/news",
            base_url="https://example.com",
            source_type="archive_page",
            metadata={"exclude_contains": ["/video/"]},
        )
        html = b"""
        <html><body>
          <a href="/news/world/story-1">World story one</a>
          <a href="/video/clip-1">Video clip</a>
          <a href="/news/world/story-2">World story two</a>
        </body></html>
        """
        links = parse_archive_page_bytes(html, source, max_links=5)
        self.assertEqual([link.url for link in links], [
            "https://example.com/news/world/story-1",
            "https://example.com/news/world/story-2",
        ])

    def test_parse_article_page_extracts_title_body_and_date(self) -> None:
        source = Source(
            slug="archive",
            name="Archive",
            feed_url="https://example.com/news",
            base_url="https://example.com",
            source_type="archive_page",
            language="en",
            region="uae",
        )
        html = b"""
        <html>
          <head>
            <title>Ignored title</title>
            <meta property="og:title" content="Story Title">
            <meta name="description" content="Story summary">
            <meta property="article:published_time" content="2026-04-09T10:00:00Z">
          </head>
          <body>
            <article>
              <p>This is the first body paragraph with enough text to be captured by the parser.</p>
              <p>This is the second body paragraph with additional context about the same story.</p>
            </article>
          </body>
        </html>
        """
        article = parse_article_page_bytes(html, source, article_url="https://example.com/news/story-1")
        assert article is not None
        self.assertEqual(article.title, "Story Title")
        self.assertEqual(article.region_tags, ["uae"])
        self.assertIn("first body paragraph", article.body)
        self.assertEqual(article.published_at.tzinfo, UTC)

    def test_browser_required_without_fallback_reports_failure(self) -> None:
        source = Source(
            slug="js-site",
            name="JS Site",
            feed_url="https://example.com/home",
            base_url="https://example.com",
            source_type="archive_page",
            metadata={"use_browser": True, "allow_heuristic_fallback": False},
        )
        config = AppConfig(browser=BrowserConfig(enabled=True))
        with patch("newy.browser_fetcher.BrowserPageRenderer.available", return_value=False):
            result = fetch_archive_page_result(source, config)
        self.assertEqual(result.status, "navigation_exhausted")
        self.assertEqual(result.articles, [])
        self.assertTrue(any("browser_required_unavailable" in error for error in result.trace["errors"]))

    def test_invalid_codex_actions_are_ignored_and_heuristics_still_work(self) -> None:
        source = Source(
            slug="archive",
            name="Archive",
            feed_url="https://example.com/home",
            base_url="https://example.com",
            source_type="archive_page",
            language="en",
            region="global",
            metadata={"max_links": 2, "max_navigation_steps": 1},
        )
        config = AppConfig(codex=CodexConfig(enabled=True, command="codex", working_directory="."))
        pages = {
            "https://example.com/home": b"""
                <html><body>
                  <a href="/news/story-1">Story One</a>
                </body></html>
            """,
            "https://example.com/news/story-1": b"""
                <html><head>
                  <meta property="og:title" content="Heuristic rescue story">
                  <meta property="article:published_time" content="2026-04-09T10:00:00Z">
                </head>
                <body><p>This article was reached through heuristic fallback after invalid Codex actions.</p></body></html>
            """,
        }

        def fake_load_page(url, browser_session, trace):
            return pages[url].decode("utf-8")

        def fake_run(cmd, input, text, capture_output, timeout, cwd):
            output_path = Path(cmd[cmd.index("-o") + 1])
            output_path.write_text(json.dumps({
                "actions": [
                    {"kind": "open_article", "title": "Hallucinated", "url": "https://example.com/not-present", "reason": "bad"}
                ]
            }))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("newy.navigation_agent.BrowserTaskNavigator._load_page", side_effect=fake_load_page), patch(
            "newy.config.shutil.which", return_value="/opt/homebrew/bin/codex"
        ), patch("newy.navigation_agent.subprocess.run", side_effect=fake_run):
            result = fetch_archive_page_result(source, config)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.articles[0].title, "Heuristic rescue story")

    def test_agent_navigation_can_follow_intermediate_page_to_article(self) -> None:
        source = Source(
            slug="archive",
            name="Archive",
            feed_url="https://example.com/home",
            base_url="https://example.com",
            source_type="archive_page",
            language="en",
            region="global",
            metadata={"max_links": 3, "max_navigation_steps": 2},
        )
        config = AppConfig(codex=CodexConfig(enabled=True, command="codex", working_directory="."))

        pages = {
            "https://example.com/home": """
                <html><body>
                  <a href="/sections/world">World Section</a>
                  <a href="/about">About</a>
                </body></html>
            """,
            "https://example.com/sections/world": """
                <html><body>
                  <a href="/news/story-1">Story One</a>
                </body></html>
            """,
            "https://example.com/news/story-1": """
                <html><head>
                  <meta property="og:title" content="Agent navigated story">
                  <meta property="article:published_time" content="2026-04-09T10:00:00Z">
                </head>
                <body><p>This article was reached after navigating from the home page to a section page.</p></body></html>
            """,
        }

        def fake_load_page(url, browser_session, trace):
            return pages[url]

        decisions = iter([
            {"actions": [{"kind": "open_navigation", "title": "World Section", "url": "https://example.com/sections/world", "reason": "section page"}]},
            {"actions": [{"kind": "open_article", "title": "Story One", "url": "https://example.com/news/story-1", "reason": "article"}]},
        ])

        def fake_run(cmd, input, text, capture_output, timeout, cwd):
            output_path = Path(cmd[cmd.index("-o") + 1])
            output_path.write_text(json.dumps(next(decisions)))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("newy.navigation_agent.BrowserTaskNavigator._load_page", side_effect=fake_load_page), patch(
            "newy.config.shutil.which", return_value="/opt/homebrew/bin/codex"
        ), patch("newy.navigation_agent.subprocess.run", side_effect=fake_run):
            result = fetch_archive_page_result(source, config)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.articles[0].title, "Agent navigated story")
        self.assertIn("https://example.com/news/story-1", result.trace["validated_articles"])
