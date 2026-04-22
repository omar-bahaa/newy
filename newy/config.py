from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CodexConfig:
    enabled: bool = True
    command: str = "codex"
    profile: str = ""
    model: str = ""
    timeout_seconds: int = 120
    working_directory: str = "."

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        return shutil.which(self.command) is not None or Path(self.command).exists()


@dataclass(slots=True)
class LLMConfig:
    base_url: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4.1-mini"
    timeout_seconds: int = 45

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and os.getenv(self.api_key_env))

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "")


@dataclass(slots=True)
class BrowserConfig:
    enabled: bool = False
    engine: str = "chromium"
    headless: bool = True
    timeout_seconds: int = 30
    wait_until: str = "networkidle"


@dataclass(slots=True)
class TwilioConfig:
    account_sid_env: str = "TWILIO_ACCOUNT_SID"
    auth_token_env: str = "TWILIO_AUTH_TOKEN"
    from_number: str = ""
    dry_run: bool = True
    validate_signature: bool = False

    @property
    def account_sid(self) -> str:
        return os.getenv(self.account_sid_env, "")

    @property
    def auth_token(self) -> str:
        return os.getenv(self.auth_token_env, "")


@dataclass(slots=True)
class AppConfig:
    database_path: str = "data/newy.sqlite3"
    source_seed_path: str = "data/sources.seed.json"
    fallback_domains: list[str] = field(default_factory=list)
    admin_host: str = "127.0.0.1"
    admin_port: int = 8080
    admin_token: str = ""
    public_base_url: str = ""
    worker_poll_interval_seconds: int = 30
    user_agent: str = "newy/0.1"
    http_timeout_seconds: int = 20
    max_article_age_hours: int = 48
    max_archive_links_per_source: int = 8
    max_articles_per_digest: int = 8
    max_clusters_for_llm: int = 8
    max_navigation_actions_per_page: int = 6
    max_navigation_retries_per_page: int = 2
    llm: LLMConfig = field(default_factory=LLMConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    twilio: TwilioConfig = field(default_factory=TwilioConfig)
    timezone_default: str = "UTC"
    _root_dir: str = field(default=".", init=False, repr=False)

    def resolve_path(self, path: str) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.root_dir / value

    @property
    def root_dir(self) -> Path:
        return Path(self._root_dir)

    @classmethod
    def load(cls, path: str | None = None) -> "AppConfig":
        config_path = path or os.getenv("NEWY_CONFIG")
        payload: dict[str, Any] = {}
        root_dir = Path.cwd()
        defaults = cls()
        if config_path:
            p = Path(config_path).expanduser().resolve()
            payload = json.loads(p.read_text())
            root_dir = p.parent

        cfg = cls(
            database_path=payload.get("database_path", defaults.database_path),
            source_seed_path=payload.get("source_seed_path", defaults.source_seed_path),
            fallback_domains=payload.get("fallback_domains", []),
            admin_host=payload.get("admin_host", defaults.admin_host),
            admin_port=int(payload.get("admin_port", defaults.admin_port)),
            admin_token=payload.get("admin_token", os.getenv("NEWY_ADMIN_TOKEN", "")),
            public_base_url=payload.get("public_base_url", defaults.public_base_url),
            worker_poll_interval_seconds=int(payload.get("worker_poll_interval_seconds", defaults.worker_poll_interval_seconds)),
            user_agent=payload.get("user_agent", defaults.user_agent),
            http_timeout_seconds=int(payload.get("http_timeout_seconds", defaults.http_timeout_seconds)),
            max_article_age_hours=int(payload.get("max_article_age_hours", defaults.max_article_age_hours)),
            max_archive_links_per_source=int(payload.get("max_archive_links_per_source", defaults.max_archive_links_per_source)),
            max_articles_per_digest=int(payload.get("max_articles_per_digest", defaults.max_articles_per_digest)),
            max_clusters_for_llm=int(payload.get("max_clusters_for_llm", defaults.max_clusters_for_llm)),
            max_navigation_actions_per_page=int(payload.get("max_navigation_actions_per_page", defaults.max_navigation_actions_per_page)),
            max_navigation_retries_per_page=int(payload.get("max_navigation_retries_per_page", defaults.max_navigation_retries_per_page)),
            llm=LLMConfig(**payload.get("llm", {})),
            codex=CodexConfig(**payload.get("codex", {})),
            browser=BrowserConfig(**payload.get("browser", {})),
            twilio=TwilioConfig(**payload.get("twilio", {})),
            timezone_default=payload.get("timezone_default", defaults.timezone_default),
        )
        cfg._root_dir = str(root_dir)
        return cfg
