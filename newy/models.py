from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Source:
    slug: str
    name: str
    feed_url: str
    base_url: str = ""
    region: str = "global"
    language: str = "en"
    trust_tier: int = 3
    source_type: str = "rss"
    active: bool = True
    poll_interval_minutes: int = 30
    fallback_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Article:
    source_slug: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    body: str = ""
    author: str = ""
    language: str = "en"
    topic_tags: list[str] = field(default_factory=list)
    region_tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class User:
    id: int | None
    name: str
    phone_number: str
    preferred_language: str = "bilingual"
    topics: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    active: bool = True


@dataclass(slots=True)
class DigestRequest:
    kind: str
    language: str
    user_id: int | None = None
    topic: str = ""
    regions: list[str] = field(default_factory=list)
    user_topics: list[str] = field(default_factory=list)
    lookback_hours: int = 24
    max_items: int = 8
    allow_fallback: bool = True


@dataclass(slots=True)
class DigestBullet:
    text: str
    citations: list[str]
    source_names: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class Digest:
    title: str
    language: str
    bullets: list[DigestBullet]
    why_it_matters: str
    citations: list[str]
    confidence: float
    created_at: datetime
    topic: str = ""
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class SourceFetchResult:
    articles: list[Article]
    status: str
    error: str = ""
    trace: dict[str, Any] = field(default_factory=dict)
