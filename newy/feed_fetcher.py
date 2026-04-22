from __future__ import annotations

import urllib.request

from .config import AppConfig
from .models import Source, SourceFetchResult
from .navigation_agent import BrowserTaskNavigator
from .page_extractors import parse_archive_page_bytes, parse_article_page_bytes, parse_rss_bytes


def fetch_url(url: str, config: AppConfig, *, accept: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": config.user_agent,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=config.http_timeout_seconds) as response:
        return response.read()


def fetch_source_result(source: Source, config: AppConfig) -> SourceFetchResult:
    source_type = (source.source_type or "rss").lower()
    if source_type in {"rss", "atom"}:
        return fetch_rss_result(source, config)
    if source_type in {"archive_page", "newsletter_archive"}:
        return fetch_archive_page_result(source, config)
    raise ValueError(f"Unsupported source type: {source.source_type}")


def fetch_rss_result(source: Source, config: AppConfig) -> SourceFetchResult:
    data = fetch_url(
        source.feed_url,
        config,
        accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
    )
    articles = parse_rss_bytes(data, source)
    return SourceFetchResult(
        articles=articles,
        status="success" if articles else "no_article_found",
        trace={
            "mode": "rss",
            "feed_url": source.feed_url,
            "articles_found": len(articles),
        },
    )


def fetch_archive_page_result(source: Source, config: AppConfig) -> SourceFetchResult:
    navigator = BrowserTaskNavigator(source, config)
    result = navigator.run()
    error = ""
    if result.status not in {"success", "partial_success"}:
        error = result.status
    return SourceFetchResult(
        articles=result.articles,
        status=result.status,
        error=error,
        trace=result.trace.as_dict(),
    )


__all__ = [
    "fetch_archive_page_result",
    "fetch_rss_result",
    "fetch_source_result",
    "fetch_url",
    "parse_archive_page_bytes",
    "parse_article_page_bytes",
    "parse_rss_bytes",
]
