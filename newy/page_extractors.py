from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .models import Article, Source


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
SKIP_URL_PREFIXES = ("mailto:", "javascript:", "tel:", "#")
META_PUBLISHED_KEYS = {
    "article:published_time",
    "og:published_time",
    "publish-date",
    "date",
    "parsely-pub-date",
}


@dataclass(slots=True)
class ArchiveLink:
    title: str
    url: str


class ArchiveHTMLParser(HTMLParser):
    def __init__(self, source: Source, max_links: int):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.max_links = max_links
        self.current_href = ""
        self.current_text: list[str] = []
        self.links: list[ArchiveLink] = []
        self.seen: set[str] = set()

        metadata = source.metadata or {}
        self.allowed_hosts = {
            urlsplit(source.base_url or source.feed_url).netloc.lower(),
            urlsplit(source.feed_url).netloc.lower(),
        }
        self.link_prefixes = tuple(metadata.get("link_prefixes", []))
        self.link_contains = tuple(metadata.get("link_contains", []))
        self.exclude_contains = tuple(metadata.get("exclude_contains", []))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        href = attr_map.get("href", "").strip()
        if not href:
            return
        self.current_href = href
        self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self.current_href:
            return
        title = normalize_text(" ".join(self.current_text))
        href = self.current_href
        self.current_href = ""
        self.current_text = []
        if not title or len(title) < 12:
            return
        absolute_url = canonicalize_url(urljoin(self.source.base_url or self.source.feed_url, href))
        if not is_allowed_candidate_url(absolute_url, self.allowed_hosts, self.exclude_contains):
            return
        parts = urlsplit(absolute_url)
        if self.link_prefixes and not any(parts.path.startswith(prefix) for prefix in self.link_prefixes):
            return
        if self.link_contains and not any(token in absolute_url for token in self.link_contains):
            return
        if parts.path.count("/") < 2 or absolute_url in self.seen:
            return
        self.seen.add(absolute_url)
        self.links.append(ArchiveLink(title=title, url=absolute_url))
        if len(self.links) >= self.max_links:
            raise StopIteration


class CandidateLinkParser(HTMLParser):
    def __init__(self, source: Source, current_url: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.current_url = current_url
        self.current_href = ""
        self.current_text: list[str] = []
        self.links: list[ArchiveLink] = []
        self.seen: set[str] = set()
        metadata = source.metadata or {}
        self.allowed_hosts = {
            urlsplit(source.base_url or source.feed_url).netloc.lower(),
            urlsplit(source.feed_url).netloc.lower(),
        }
        self.exclude_contains = tuple(metadata.get("exclude_contains", []))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        href = attr_map.get("href", "").strip()
        if not href:
            return
        self.current_href = href
        self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self.current_href:
            return
        title = normalize_text(" ".join(self.current_text))
        href = self.current_href
        self.current_href = ""
        self.current_text = []
        absolute_url = canonicalize_url(urljoin(self.current_url, href))
        if not title or len(title) < 6:
            return
        if absolute_url in self.seen:
            return
        if not is_allowed_candidate_url(absolute_url, self.allowed_hosts, self.exclude_contains):
            return
        self.seen.add(absolute_url)
        self.links.append(ArchiveLink(title=title, url=absolute_url))


class ArticleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.in_script = False
        self.in_style = False
        self.in_paragraph = False
        self.in_ignored_section = False
        self.current_paragraph: list[str] = []
        self.paragraphs: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        elif tag in {"script", "style", "noscript"}:
            self.in_script = True
            self.in_style = True
        elif tag in {"nav", "footer", "aside"}:
            self.in_ignored_section = True
        elif tag == "meta":
            prop = (attr_map.get("property") or attr_map.get("name") or "").lower()
            content = attr_map.get("content", "")
            if prop and content:
                self.meta[prop] = normalize_text(content)
        elif tag == "time":
            dt = attr_map.get("datetime", "")
            if dt:
                self.meta.setdefault("published_time", dt)
        elif tag == "p" and not self.in_ignored_section and not self.in_script and not self.in_style:
            self.in_paragraph = True
            self.current_paragraph = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        elif tag in {"script", "style", "noscript"}:
            self.in_script = False
            self.in_style = False
        elif tag in {"nav", "footer", "aside"}:
            self.in_ignored_section = False
        elif tag == "p" and self.in_paragraph:
            paragraph = normalize_text(" ".join(self.current_paragraph))
            if len(paragraph) >= 40 and paragraph not in self.paragraphs:
                self.paragraphs.append(paragraph)
            self.current_paragraph = []
            self.in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif self.in_paragraph:
            self.current_paragraph.append(data)


def normalize_text(value: str) -> str:
    value = unescape(value or "")
    value = TAG_RE.sub(" ", value)
    value = WS_RE.sub(" ", value).strip()
    return value


def parse_timestamp(*values: str) -> datetime:
    for raw in values:
        if not raw:
            continue
        raw = raw.strip()
        try:
            if raw.endswith("Z"):
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            try:
                return parsedate_to_datetime(raw).astimezone(UTC)
            except (TypeError, ValueError):
                continue
    return datetime.now(UTC)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme or 'https'}://{parts.netloc.lower()}{path}"


def content_hash(title: str, url: str) -> str:
    key = f"{title.lower().strip()}::{canonicalize_url(url)}"
    return hashlib.sha256(key.encode()).hexdigest()


def is_allowed_candidate_url(url: str, allowed_hosts: set[str], exclude_contains: tuple[str, ...] = ()) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.netloc.lower() not in allowed_hosts:
        return False
    if any(url.startswith(prefix) for prefix in SKIP_URL_PREFIXES):
        return False
    if exclude_contains and any(token in url for token in exclude_contains):
        return False
    return True


def parse_rss_bytes(data: bytes, source: Source) -> list[Article]:
    lowered = data.lower()
    if b"<!entity" in lowered:
        raise ValueError(f"Rejected unsafe XML payload from source {source.slug}")
    root = ET.fromstring(data)
    if root.tag.endswith("feed"):
        return _parse_atom(root, source)
    return _parse_rss(root, source)


def parse_archive_page_bytes(data: bytes, source: Source, *, max_links: int = 8) -> list[ArchiveLink]:
    parser = ArchiveHTMLParser(source, max_links=max_links)
    try:
        parser.feed(data.decode("utf-8", errors="ignore"))
    except StopIteration:
        pass
    return parser.links



def extract_candidate_links(html_text: str, source: Source, current_url: str) -> list[ArchiveLink]:
    parser = CandidateLinkParser(source, current_url)
    parser.feed(html_text)
    return parser.links


def parse_article_page_bytes(
    data: bytes,
    source: Source,
    *,
    article_url: str,
    fallback_title: str = "",
) -> Article | None:
    html = data.decode("utf-8", errors="ignore")
    parser = ArticleHTMLParser()
    parser.feed(html)
    title = normalize_text(parser.meta.get("og:title") or parser.title or fallback_title)
    if not title:
        return None
    summary = normalize_text(parser.meta.get("description") or parser.meta.get("og:description") or "")
    published = parse_timestamp(parser.meta.get("published_time", ""), *(parser.meta.get(key, "") for key in META_PUBLISHED_KEYS))
    body = "\n\n".join(parser.paragraphs[:8])
    if not body and summary:
        body = summary
    if len(body) < 40 and not summary:
        return None
    return Article(
        source_slug=source.slug,
        title=title,
        url=canonicalize_url(article_url),
        summary=summary or body[:240],
        body=body,
        author="",
        language=source.language,
        published_at=published,
        region_tags=[source.region],
        metadata={
            "content_hash": content_hash(title, article_url),
            "host": urlsplit(article_url).netloc.lower(),
            "source_type": source.source_type,
        },
    )


def parse_article_html(html_text: str, *, source: Source, url: str, fallback_title: str = "") -> Article | None:
    return parse_article_page_bytes(html_text.encode("utf-8"), source, article_url=url, fallback_title=fallback_title)


def _parse_rss(root: ET.Element, source: Source) -> list[Article]:
    articles: list[Article] = []
    for item in root.findall("./channel/item"):
        title = normalize_text(item.findtext("title", ""))
        url = normalize_text(item.findtext("link", ""))
        summary = normalize_text(item.findtext("description", ""))
        if not title or not url:
            continue
        pub_date = parse_timestamp(
            item.findtext("pubDate", ""),
            item.findtext("published", ""),
            item.findtext("updated", ""),
        )
        articles.append(
            Article(
                source_slug=source.slug,
                title=title,
                url=canonicalize_url(url),
                summary=summary,
                body=summary,
                author=normalize_text(item.findtext("author", "")),
                language=source.language,
                published_at=pub_date,
                region_tags=[source.region],
                metadata={
                    "content_hash": content_hash(title, url),
                    "host": urlsplit(url).netloc.lower(),
                    "source_type": source.source_type,
                },
            )
        )
    return articles


def _find_first_text(entry: ET.Element, *tags: str) -> str:
    for child in entry:
        if child.tag.split("}")[-1] in tags:
            return normalize_text(child.text or "")
    return ""


def _parse_atom(root: ET.Element, source: Source) -> list[Article]:
    entries = [node for node in root if node.tag.split("}")[-1] == "entry"]
    articles: list[Article] = []
    for entry in entries:
        title = _find_first_text(entry, "title")
        link = ""
        for child in entry:
            if child.tag.split("}")[-1] == "link" and child.attrib.get("href"):
                link = child.attrib["href"]
                break
        if not title or not link:
            continue
        summary = _find_first_text(entry, "summary", "content")
        articles.append(
            Article(
                source_slug=source.slug,
                title=title,
                url=canonicalize_url(link),
                summary=summary,
                body=summary,
                author="",
                language=source.language,
                published_at=parse_timestamp(_find_first_text(entry, "published", "updated")),
                region_tags=[source.region],
                metadata={
                    "content_hash": content_hash(title, link),
                    "host": urlsplit(link).netloc.lower(),
                    "source_type": source.source_type,
                },
            )
        )
    return articles
