from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Article, DigestRequest


TOKEN_RE = re.compile(r"[A-Za-z\u0600-\u06FF0-9]{2,}")
STOPWORDS = {
    "with",
    "that",
    "from",
    "this",
    "have",
    "will",
    "about",
    "their",
    "after",
    "under",
    "into",
    "over",
    "amid",
    "against",
    "said",
    "says",
    "على",
    "من",
    "في",
    "عن",
    "الى",
    "إلى",
    "بين",
    "بعد",
    "هذا",
    "هذه",
    "هناك",
    "كانت",
    "وقال",
}


@dataclass(slots=True)
class RankedCluster:
    cluster_id: str
    articles: list[Article]
    score: float
    query_score: float


def normalize_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text or "") if token.lower() not in STOPWORDS}


def char_ngrams(text: str, n: int = 3) -> set[str]:
    collapsed = normalize_whitespace(text).lower()
    if len(collapsed) < n:
        return {collapsed} if collapsed else set()
    return {collapsed[index : index + n] for index in range(len(collapsed) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _article_text(article: Article) -> str:
    return " ".join(piece for piece in [article.title, article.summary, article.body] if piece)


def _article_similarity(left: Article, right: Article) -> float:
    token_score = _jaccard(tokenize(_article_text(left)), tokenize(_article_text(right)))
    trigram_score = _jaccard(char_ngrams(_article_text(left)), char_ngrams(_article_text(right)))
    return token_score * 0.65 + trigram_score * 0.35


def cluster_articles(articles: list[Article]) -> list[list[Article]]:
    clusters: list[list[Article]] = []
    for article in sorted(articles, key=lambda item: item.published_at, reverse=True):
        matched = None
        for cluster in clusters:
            centroid = cluster[0]
            if _article_similarity(article, centroid) >= 0.22:
                matched = cluster
                break
        if matched is None:
            clusters.append([article])
        else:
            matched.append(article)
    return clusters


def _query_similarity(query: str, cluster: list[Article]) -> float:
    if not query.strip():
        return 0.0
    cluster_text = " ".join(_article_text(article) for article in cluster[:4])
    token_score = _jaccard(tokenize(query), tokenize(cluster_text))
    trigram_score = _jaccard(char_ngrams(query), char_ngrams(cluster_text))
    return token_score * 3.0 + trigram_score * 2.0


def rank_clusters(
    clusters: list[list[Article]],
    source_trust: dict[str, int],
    request: DigestRequest,
    now: datetime | None = None,
) -> list[RankedCluster]:
    """Coarse shortlist ranking only.

    This stage should narrow noisy raw articles into a sensible shortlist.
    It should not try to make the final editorial judgment; the summarizer
    agent is responsible for final ranking/selection among these clusters.
    """
    current = now or datetime.now(UTC)
    query = request.topic or " ".join(request.user_topics)
    ranked: list[RankedCluster] = []
    for index, cluster in enumerate(clusters, start=1):
        source_count = len({article.source_slug for article in cluster})
        max_trust = max(source_trust.get(article.source_slug, 1) for article in cluster)
        oldest_hours = max((current - article.published_at).total_seconds() / 3600 for article in cluster)
        freshness = max(0.0, 3.5 - min(oldest_hours, 48) / 16)
        corroboration = min(source_count, 4) * 0.9
        region_bonus = 0.0
        if request.regions and any(set(request.regions) & set(article.region_tags) for article in cluster):
            region_bonus = 1.2
        query_score = _query_similarity(query, cluster)
        density = math.log(len(cluster) + 1, 2)
        coarse_score = max_trust * 1.5 + freshness + corroboration + region_bonus + density + min(query_score, 1.5)
        ranked.append(
            RankedCluster(
                cluster_id=f"cluster-{index}",
                articles=cluster,
                score=coarse_score,
                query_score=query_score,
            )
        )
    return sorted(ranked, key=lambda item: item.score, reverse=True)
