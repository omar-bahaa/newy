from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .config import AppConfig
from .models import Article, Digest, DigestBullet, DigestRequest
from .ranking import RankedCluster, cluster_articles, rank_clusters


class ProviderError(RuntimeError):
    pass


class SummarizerProvider(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def summarize(self, request: DigestRequest, clusters: list[RankedCluster]) -> Digest: ...


@dataclass(slots=True)
class EvidenceBundle:
    clusters: list[RankedCluster]
    total_articles: int


class DigestEngine:
    def __init__(self, config: AppConfig, providers: list[SummarizerProvider] | None = None):
        self.config = config
        self.providers = providers or [
            CodexLocalProvider(config),
            OpenAIChatProvider(config),
            ExtractiveProvider(config),
        ]

    def build_digest(self, request: DigestRequest, articles: list[Article], source_trust: dict[str, int]) -> Digest:
        evidence = self._retrieve_and_curate(request, articles, source_trust)
        if not evidence.clusters:
            return self._empty_digest(request)

        last_error: Exception | None = None
        for provider in self.providers:
            if not provider.is_available():
                continue
            try:
                digest = provider.summarize(request, evidence.clusters)
                return self._verify_digest(digest, evidence, request)
            except Exception as exc:  # pragma: no cover - exercised through fallback path
                last_error = exc
                continue

        if last_error:
            raise ProviderError(f"No summarizer provider succeeded: {last_error}")
        return self._empty_digest(request)

    def _retrieve_and_curate(
        self,
        request: DigestRequest,
        articles: list[Article],
        source_trust: dict[str, int],
    ) -> EvidenceBundle:
        recent_cutoff = datetime.now(UTC) - timedelta(hours=request.lookback_hours)
        filtered = [article for article in articles if article.published_at >= recent_cutoff]
        if request.regions:
            regional = [article for article in filtered if set(request.regions) & set(article.region_tags)]
            if regional:
                filtered = regional
        ranked = rank_clusters(cluster_articles(filtered), source_trust, request)
        cluster_limit = max(request.max_items * 2, self.config.max_clusters_for_llm)
        return EvidenceBundle(clusters=ranked[:cluster_limit], total_articles=len(filtered))

    def _verify_digest(self, digest: Digest, evidence: EvidenceBundle, request: DigestRequest) -> Digest:
        valid_urls = {article.url for cluster in evidence.clusters for article in cluster.articles}
        normalized_sections: dict[str, dict[str, object]] = {}
        aggregate_citations: list[str] = []
        primary_bullets: list[DigestBullet] = []

        for language in ("en", "ar"):
            section = digest.sections.get(language) or {}
            clean_bullets: list[dict[str, object]] = []
            for item in section.get("bullets", []):
                if isinstance(item, str):
                    text = " ".join(item.split())
                    citations: list[str] = []
                else:
                    text = " ".join(str(item.get("text", "")).split())
                    citations = [url for url in item.get("citations", []) if url in valid_urls]
                if not text or not citations:
                    continue
                clean_bullets.append({"text": text, "citations": citations[:3]})
                aggregate_citations.extend(citations[:3])
                if language == "en":
                    primary_bullets.append(DigestBullet(text=text, citations=citations[:3], confidence=digest.confidence or 0.8))
            normalized_sections[language] = {
                "title": section.get("title") or self._section_title(request, language),
                "why": section.get("why") or self._why_default(request, language),
                "bullets": clean_bullets,
                "citations": list(dict.fromkeys(aggregate_citations)),
            }

        if not primary_bullets and normalized_sections["ar"]["bullets"]:
            primary_bullets = [
                DigestBullet(
                    text=item["text"],
                    citations=list(item["citations"]),
                    confidence=digest.confidence or 0.8,
                )
                for item in normalized_sections["ar"]["bullets"]
            ]
        if not primary_bullets:
            raise ProviderError("digest provider returned no verifiable bullets")

        citations = list(dict.fromkeys(aggregate_citations))
        if not citations:
            raise ProviderError("digest provider returned no valid citations")
        normalized_sections["en"]["citations"] = citations
        normalized_sections["ar"]["citations"] = citations
        return Digest(
            title=str(normalized_sections["en"]["title"]),
            language=request.language,
            bullets=primary_bullets,
            why_it_matters=str(normalized_sections["en"]["why"]),
            citations=citations,
            confidence=max(0.1, min(float(digest.confidence or 0.75), 0.99)),
            created_at=datetime.now(UTC),
            topic=request.topic,
            sections=normalized_sections,
        )

    def _section_title(self, request: DigestRequest, language: str) -> str:
        if language == "ar":
            return "الملخص اليومي" if request.kind == "daily" else f"ملخص الموضوع: {request.topic}"
        return "Daily digest" if request.kind == "daily" else f"Topic digest: {request.topic}"

    def _why_default(self, request: DigestRequest, language: str) -> str:
        if language == "ar":
            return "تم إعداد هذا الملخص من المصادر المتاحة الأكثر موثوقية وحداثة ضمن نافذة التغطية المحددة."
        return "This digest is grounded in the most recent and corroborated retrieved sources within the selected coverage window."

    def _empty_digest(self, request: DigestRequest) -> Digest:
        en_title = self._section_title(request, "en")
        ar_title = self._section_title(request, "ar")
        en_message = "Insufficient trusted coverage for this request in the selected time window."
        ar_message = "لا توجد تغطية موثوقة كافية لهذا الطلب ضمن النافذة الزمنية المحددة."
        sections = {
            "en": {
                "title": en_title,
                "bullets": [{"text": en_message, "citations": []}],
                "why": "Try broadening the topic, increasing the time window, or adding more trusted sources.",
                "citations": [],
            },
            "ar": {
                "title": ar_title,
                "bullets": [{"text": ar_message, "citations": []}],
                "why": "يمكن توسيع الموضوع أو زيادة النافذة الزمنية أو إضافة مصادر موثوقة أخرى.",
                "citations": [],
            },
        }
        return Digest(
            title=en_title,
            language=request.language,
            bullets=[DigestBullet(text=en_message, citations=[])],
            why_it_matters=sections["en"]["why"],
            citations=[],
            confidence=0.1,
            created_at=datetime.now(UTC),
            topic=request.topic,
            sections=sections,
        )


class CodexLocalProvider:
    name = "codex_local"

    def __init__(self, config: AppConfig):
        self.config = config

    def is_available(self) -> bool:
        return self.config.codex.available

    def summarize(self, request: DigestRequest, clusters: list[RankedCluster]) -> Digest:
        prompt = self._build_prompt(request, clusters)
        schema = self._response_schema()
        workdir = self.config.resolve_path(self.config.codex.working_directory)
        workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="newy-codex-") as tmp:
            schema_path = f"{tmp}/schema.json"
            output_path = f"{tmp}/output.json"
            with open(schema_path, "w", encoding="utf-8") as handle:
                json.dump(schema, handle)
            cmd = [
                self.config.codex.command,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--output-schema",
                schema_path,
                "-o",
                output_path,
                "-C",
                str(workdir),
            ]
            if self.config.codex.profile:
                cmd.extend(["--profile", self.config.codex.profile])
            if self.config.codex.model:
                cmd.extend(["--model", self.config.codex.model])
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.config.codex.timeout_seconds,
                cwd=workdir,
            )
            if result.returncode != 0:
                raise ProviderError(result.stderr.strip() or result.stdout.strip() or "codex exec failed")
            with open(output_path, encoding="utf-8") as handle:
                content = handle.read().strip()
            if not content:
                raise ProviderError("codex exec returned empty output")
        parsed = json.loads(content)
        return digest_from_sections(request, parsed)

    def _build_prompt(self, request: DigestRequest, clusters: list[RankedCluster]) -> str:
        evidence = []
        for coarse_rank, cluster in enumerate(clusters, start=1):
            evidence.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "coarse_rank": coarse_rank,
                    "coarse_score": round(cluster.score, 3),
                    "query_hint": round(cluster.query_score, 3),
                    "articles": [
                        {
                            "source": article.source_slug,
                            "title": article.title,
                            "summary": article.summary,
                            "body_excerpt": article.body[:500],
                            "url": article.url,
                            "published_at": article.published_at.isoformat(),
                            "language": article.language,
                        }
                        for article in cluster.articles[:4]
                    ],
                }
            )
        instruction = {
            "task": "You are the final editorial ranker and digest judge for a news digest.",
            "requirements": [
                "Treat coarse_rank and coarse_score as shortlist hints only, not final truth.",
                "You must perform the final ranking and decide which shortlisted clusters deserve inclusion.",
                "Use only claims directly supported by the evidence.",
                "Citations must be exact URLs copied from the evidence.",
                "For topic digests, include only developments meaningfully tied to the topic, even if wording differs from the query.",
                "Prefer the most important, corroborated, and current developments, not just the highest coarse-ranked cluster.",
                "It is acceptable to ignore low-value shortlisted clusters.",
                "Return only JSON matching the provided schema.",
            ],
            "request": {
                "kind": request.kind,
                "topic": request.topic,
                "language": request.language,
                "regions": request.regions,
                "user_topics": request.user_topics,
                "target_bullets": request.max_items,
            },
            "shortlist": evidence,
        }
        return json.dumps(instruction, ensure_ascii=False, indent=2)

    def _response_schema(self) -> dict[str, object]:
        section_schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "why": {"type": "string"},
                "bullets": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": self.config.max_articles_per_digest,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "citations": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["text", "citations"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["title", "why", "bullets"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {
                "en": section_schema,
                "ar": section_schema,
                "confidence": {"type": "number"},
            },
            "required": ["en", "ar", "confidence"],
            "additionalProperties": False,
        }


class OpenAIChatProvider:
    name = "openai_chat"

    def __init__(self, config: AppConfig):
        self.config = config

    def is_available(self) -> bool:
        return self.config.llm.enabled

    def summarize(self, request: DigestRequest, clusters: list[RankedCluster]) -> Digest:
        payload = {
            "model": self.config.llm.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the final editorial ranker and digest judge for a citation-grounded news digest. "
                        "Use the supplied shortlist as input, perform the final ranking yourself, and return JSON with keys en, ar, confidence. "
                        "Each section must include title, why, bullets[]. Each bullet must include text and citations."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": {
                                "kind": request.kind,
                                "topic": request.topic,
                                "language": request.language,
                                "regions": request.regions,
                                "target_bullets": request.max_items,
                            },
                            "shortlist": [
                                {
                                    "cluster_id": cluster.cluster_id,
                                    "coarse_rank": coarse_rank,
                                    "coarse_score": cluster.score,
                                    "query_hint": cluster.query_score,
                                    "articles": [
                                        {
                                            "source": article.source_slug,
                                            "title": article.title,
                                            "summary": article.summary,
                                            "body_excerpt": article.body[:500],
                                            "url": article.url,
                                            "published_at": article.published_at.isoformat(),
                                        }
                                        for article in cluster.articles[:4]
                                    ],
                                }
                                for coarse_rank, cluster in enumerate(clusters, start=1)
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        request_obj = urllib.request.Request(
            self.config.llm.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.llm.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request_obj, timeout=self.config.llm.timeout_seconds) as response:
            body = json.loads(response.read().decode())
        parsed = json.loads(body["choices"][0]["message"]["content"])
        return digest_from_sections(request, parsed)


class ExtractiveProvider:
    name = "extractive"

    def __init__(self, config: AppConfig):
        self.config = config

    def is_available(self) -> bool:
        return True

    def summarize(self, request: DigestRequest, clusters: list[RankedCluster]) -> Digest:
        selected = clusters[: request.max_items]
        en_bullets: list[dict[str, object]] = []
        ar_bullets: list[dict[str, object]] = []
        citations: list[str] = []

        for cluster in selected:
            primary = cluster.articles[0]
            corroborating_sources = len({article.source_slug for article in cluster.articles})
            bullet_citations = [article.url for article in cluster.articles[:3]]
            citations.extend(bullet_citations)
            en_bullets.append(
                {
                    "text": self._english_line(primary, corroborating_sources),
                    "citations": bullet_citations,
                }
            )
            ar_bullets.append(
                {
                    "text": self._arabic_line(primary, corroborating_sources),
                    "citations": bullet_citations,
                }
            )
        parsed = {
            "en": {
                "title": "Daily digest" if request.kind == "daily" else f"Topic digest: {request.topic}",
                "why": self._why_it_matters(request, "en", len(selected)),
                "bullets": en_bullets,
            },
            "ar": {
                "title": "الملخص اليومي" if request.kind == "daily" else f"ملخص الموضوع: {request.topic}",
                "why": self._why_it_matters(request, "ar", len(selected)),
                "bullets": ar_bullets,
            },
            "confidence": min(0.85, 0.4 + len(selected) * 0.08),
        }
        return digest_from_sections(request, parsed)

    def _english_line(self, article: Article, corroborating_sources: int) -> str:
        summary = (article.summary or article.body or article.title).strip().rstrip(".")
        if len(summary) > 190:
            summary = summary[:187].rsplit(" ", 1)[0] + "..."
        source_label = "source" if corroborating_sources == 1 else "sources"
        return f"{article.title} — {summary} (supported by {corroborating_sources} {source_label})."

    def _arabic_line(self, article: Article, corroborating_sources: int) -> str:
        if article.language.lower().startswith("ar"):
            snippet = (article.summary or article.body or article.title).strip().rstrip(".")
            if len(snippet) > 190:
                snippet = snippet[:187].rsplit(" ", 1)[0] + "..."
            return f"{article.title} — {snippet} (مدعوم عبر {corroborating_sources} مصدر)."
        return f"{article.title} — تطور رئيسي مدعوم عبر {corroborating_sources} مصدر مع روابط مرجعية مرفقة."

    def _why_it_matters(self, request: DigestRequest, language: str, bullet_count: int) -> str:
        if language == "ar":
            if request.kind == "topic" and request.topic:
                return f"يركز هذا الملخص على آخر التطورات المرتبطة بموضوع {request.topic} اعتماداً على أكثر المصادر دعماً وتوافقاً."
            return f"يركز هذا الملخص على الأخبار الأحدث والأكثر دعماً عبر {bullet_count} مجموعات خبرية رئيسية."
        if request.kind == "topic" and request.topic:
            return f"This digest focuses on the most corroborated developments related to {request.topic}."
        return f"This digest prioritizes the most recent and corroborated developments across {bullet_count} major story clusters."


def digest_from_sections(request: DigestRequest, payload: dict[str, object]) -> Digest:
    en = payload.get("en", {})
    ar = payload.get("ar", {})
    bullets = [
        DigestBullet(text=item["text"], citations=list(item["citations"]), confidence=float(payload.get("confidence", 0.8)))
        for item in en.get("bullets", [])
    ]
    citations = list(dict.fromkeys(url for item in en.get("bullets", []) for url in item.get("citations", [])))
    return Digest(
        title=en.get("title", "Daily digest"),
        language=request.language,
        bullets=bullets,
        why_it_matters=en.get("why", ""),
        citations=citations,
        confidence=float(payload.get("confidence", 0.8)),
        created_at=datetime.now(UTC),
        topic=request.topic,
        sections={"en": en, "ar": ar},
    )
