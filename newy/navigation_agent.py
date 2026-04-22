from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from .browser_fetcher import BrowserPageRenderer, BrowserUnavailableError
from .config import AppConfig
from .models import Article, Source
from .page_extractors import (
    ArchiveLink,
    canonicalize_url,
    extract_candidate_links,
    is_allowed_candidate_url,
    parse_archive_page_bytes,
    parse_article_html,
)


@dataclass(slots=True)
class NavigationCandidate:
    title: str
    url: str
    kind: str
    origin_url: str


@dataclass(slots=True)
class NavigationAction:
    kind: str
    title: str
    url: str
    source: str
    reason: str = ""


@dataclass(slots=True)
class NavigationTrace:
    seed_url: str
    visited_pages: list[str] = field(default_factory=list)
    attempted_articles: list[str] = field(default_factory=list)
    validated_articles: list[str] = field(default_factory=list)
    actions: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    retries: int = 0
    status: str = "started"

    def add_action(self, action: NavigationAction) -> None:
        self.actions.append(
            {
                "kind": action.kind,
                "title": action.title,
                "url": action.url,
                "source": action.source,
                "reason": action.reason,
            }
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class NavigationResult:
    articles: list[Article]
    status: str
    trace: NavigationTrace


class BrowserTaskNavigator:
    def __init__(self, source: Source, config: AppConfig):
        self.source = source
        self.config = config
        self.metadata = source.metadata or {}
        self.max_links = int(self.metadata.get("max_links", config.max_archive_links_per_source))
        self.max_steps = int(self.metadata.get("max_navigation_steps", 2))
        self.max_actions = int(self.metadata.get("max_navigation_actions", getattr(config, "max_navigation_actions_per_page", 6)))
        self.max_retries = int(self.metadata.get("max_navigation_retries", getattr(config, "max_navigation_retries_per_page", 2)))
        self.allow_heuristic_fallback = bool(self.metadata.get("allow_heuristic_fallback", True))
        self.require_browser = bool(self.metadata.get("use_browser", False) or self.config.browser.enabled)
        self.allowed_hosts = {
            urlsplit(source.base_url or source.feed_url).netloc.lower(),
            urlsplit(source.feed_url).netloc.lower(),
        }
        self.exclude_contains = tuple(self.metadata.get("exclude_contains", []))

    def run(self) -> NavigationResult:
        trace = NavigationTrace(seed_url=self.source.feed_url)
        articles: list[Article] = []
        article_urls_seen: set[str] = set()
        page_urls_seen: set[str] = set()
        frontier: deque[tuple[int, str]] = deque([(0, self.source.feed_url)])
        browser_renderer = BrowserPageRenderer(self.config)

        with browser_renderer.session() as browser_session:
            while frontier and len(article_urls_seen) < self.max_links:
                depth, page_url = frontier.popleft()
                page_key = canonicalize_url(page_url)
                if page_key in page_urls_seen:
                    continue
                page_urls_seen.add(page_key)
                trace.visited_pages.append(page_url)
                try:
                    html_text = self._load_page(page_url, browser_session, trace)
                except Exception as exc:
                    trace.errors.append(f"page_load_failed:{page_url}:{exc}")
                    continue

                current_article = parse_article_html(html_text, source=self.source, url=page_url)
                if current_article and current_article.url not in article_urls_seen:
                    articles.append(current_article)
                    article_urls_seen.add(current_article.url)
                    trace.validated_articles.append(current_article.url)
                    if len(article_urls_seen) >= self.max_links:
                        break

                candidates = self._build_candidates(html_text, page_url)
                if not candidates:
                    trace.warnings.append(f"no_candidates:{page_url}")
                    continue

                actions = self._choose_actions(candidates, page_url, trace)
                for action in actions:
                    if len(article_urls_seen) >= self.max_links:
                        break
                    trace.add_action(action)
                    if action.kind == "open_article":
                        article, follow_up_url = self._open_article_candidate(action, browser_session, trace)
                        if article is not None:
                            if article.url not in article_urls_seen:
                                articles.append(article)
                                article_urls_seen.add(article.url)
                                trace.validated_articles.append(article.url)
                            continue
                        if follow_up_url and depth + 1 <= self.max_steps:
                            frontier.append((depth + 1, follow_up_url))
                    elif action.kind == "open_navigation" and depth + 1 <= self.max_steps:
                        frontier.append((depth + 1, action.url))

        if articles:
            trace.status = "success" if len(articles) >= self.max_links or not trace.errors else "partial_success"
        else:
            trace.status = "navigation_exhausted" if trace.visited_pages else "no_article_found"
        return NavigationResult(articles=articles, status=trace.status, trace=trace)

    def _load_page(self, url: str, browser_session, trace: NavigationTrace) -> str:
        if self.require_browser:
            if browser_session is None:
                message = f"browser_required_unavailable:{url}"
                trace.errors.append(message)
                if not self.allow_heuristic_fallback:
                    raise BrowserUnavailableError(message)
                trace.warnings.append(f"browser_fallback_to_http:{url}")
            else:
                return browser_session.open(url).html
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request, timeout=self.config.http_timeout_seconds) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _build_candidates(self, html_text: str, current_url: str) -> list[NavigationCandidate]:
        generic = {link.url: link for link in extract_candidate_links(html_text, self.source, current_url)}
        heuristic_articles = {link.url for link in parse_archive_page_bytes(html_text.encode("utf-8"), self.source, max_links=self.max_links)}
        heuristic_navigation = {
            link.url
            for link in generic.values()
            if any(token in urlsplit(link.url).path.lower() for token in ("/category/", "/section/", "/latest", "/news", "/world", "/uae", "/middle-east"))
        }
        candidates: list[NavigationCandidate] = []
        for link in generic.values():
            kind = "generic"
            if link.url in heuristic_articles:
                kind = "heuristic_article"
            elif link.url in heuristic_navigation:
                kind = "heuristic_navigation"
            candidates.append(NavigationCandidate(title=link.title, url=link.url, kind=kind, origin_url=current_url))
        return candidates

    def _choose_actions(self, candidates: list[NavigationCandidate], current_url: str, trace: NavigationTrace) -> list[NavigationAction]:
        actions: list[NavigationAction] = []
        if self.config.codex.available:
            try:
                actions.extend(self._choose_actions_with_codex(candidates, current_url))
            except Exception as exc:
                trace.errors.append(f"codex_navigation_failed:{current_url}:{exc}")
        if len(actions) < self.max_actions:
            actions.extend(self._heuristic_actions(candidates))
        deduped: list[NavigationAction] = []
        seen: set[tuple[str, str]] = set()
        for action in actions:
            key = (action.kind, action.url)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(action)
            if len(deduped) >= self.max_actions:
                break
        return deduped

    def _choose_actions_with_codex(self, candidates: list[NavigationCandidate], current_url: str) -> list[NavigationAction]:
        payload = {
            "task": "Choose the next best browser navigation actions for a news source page.",
            "rules": [
                "You may only choose URLs that exist in the candidate list.",
                "Prefer direct article pages first.",
                "Use navigation pages only when they plausibly lead to article pages.",
                "Do not invent URLs or titles.",
            ],
            "current_url": current_url,
            "source": {
                "slug": self.source.slug,
                "name": self.source.name,
                "language": self.source.language,
            },
            "candidates": [asdict(candidate) for candidate in candidates],
        }
        schema = {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "maxItems": self.max_actions,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["open_article", "open_navigation", "stop"]},
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["kind", "title", "url", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["actions"],
            "additionalProperties": False,
        }
        workdir = self.config.resolve_path(self.config.codex.working_directory)
        workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="newy-nav-actions-") as tmp:
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
                input=json.dumps(payload, ensure_ascii=False, indent=2),
                text=True,
                capture_output=True,
                timeout=self.config.codex.timeout_seconds,
                cwd=workdir,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "codex navigation failed")
            with open(output_path, encoding="utf-8") as handle:
                parsed = json.loads(handle.read())
        candidate_map = {candidate.url: candidate for candidate in candidates}
        actions: list[NavigationAction] = []
        for item in parsed.get("actions", []):
            if item["kind"] == "stop":
                continue
            candidate = candidate_map.get(item["url"])
            if candidate is None:
                continue
            if not is_allowed_candidate_url(candidate.url, self.allowed_hosts, self.exclude_contains):
                continue
            actions.append(
                NavigationAction(
                    kind=item["kind"],
                    title=candidate.title,
                    url=candidate.url,
                    source="codex",
                    reason=item.get("reason", ""),
                )
            )
        return actions

    def _heuristic_actions(self, candidates: Iterable[NavigationCandidate]) -> list[NavigationAction]:
        article_candidates = [candidate for candidate in candidates if candidate.kind == "heuristic_article"]
        navigation_candidates = [candidate for candidate in candidates if candidate.kind == "heuristic_navigation"]
        generic_candidates = [candidate for candidate in candidates if candidate.kind == "generic"]
        actions: list[NavigationAction] = []
        for candidate in article_candidates[: self.max_actions]:
            actions.append(NavigationAction(kind="open_article", title=candidate.title, url=candidate.url, source="heuristic", reason="heuristic_article"))
        remaining = max(self.max_actions - len(actions), 0)
        for candidate in navigation_candidates[:remaining]:
            actions.append(NavigationAction(kind="open_navigation", title=candidate.title, url=candidate.url, source="heuristic", reason="heuristic_navigation"))
        remaining = max(self.max_actions - len(actions), 0)
        for candidate in generic_candidates[:remaining]:
            actions.append(NavigationAction(kind="open_article", title=candidate.title, url=candidate.url, source="heuristic", reason="generic_article_probe"))
        return actions

    def _open_article_candidate(self, action: NavigationAction, browser_session, trace: NavigationTrace) -> tuple[Article | None, str | None]:
        if action.url in trace.attempted_articles:
            return None, None
        trace.attempted_articles.append(action.url)
        try:
            html_text = self._load_page(action.url, browser_session, trace)
        except Exception as exc:
            trace.errors.append(f"article_load_failed:{action.url}:{exc}")
            return None, None
        article = parse_article_html(html_text, source=self.source, url=action.url, fallback_title=action.title)
        if article is not None:
            return article, None
        trace.retries += 1
        if trace.retries > self.max_retries:
            trace.warnings.append(f"retry_budget_exhausted:{action.url}")
            return None, None
        return None, action.url
