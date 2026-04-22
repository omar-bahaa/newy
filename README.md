<p align="center">
  <img src="assets/newy-banner.svg" alt="Newy banner" width="100%" />
</p>

<p align="center">
  <a href="https://github.com/omar-bahaa/newy"><img src="https://img.shields.io/badge/GitHub-newy-181717?logo=github&logoColor=white" alt="GitHub"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Browser-Playwright-2EAD33?logo=playwright&logoColor=white" alt="Playwright">
  <img src="https://img.shields.io/badge/WhatsApp-Twilio-25D366?logo=whatsapp&logoColor=white" alt="Twilio WhatsApp">
  <img src="https://img.shields.io/badge/Agent-Codex-412991" alt="Codex-guided">
  <img src="https://img.shields.io/github/stars/omar-bahaa/newy?style=social" alt="GitHub stars">
</p>

# Newy

Newy is a local-first news digestion tool that pulls content from trusted sources, uses a browser task agent plus Codex to navigate article pages, generates citation-backed digests, and can deliver them to WhatsApp through Twilio.

## Highlights

- **Trusted-source ingestion** from RSS, archive pages, and newsletter archive pages
- **Browser task agent** for JS-heavy sites and multi-step page navigation
- **Codex-guided decisions** for bounded web navigation and digest generation
- **Citation-backed summaries** in English, Arabic, or bilingual output
- **Local SQLite storage** for articles, digests, deliveries, and source-run diagnostics
- **Twilio WhatsApp integration** for sandbox or production sending
- **Admin dashboard** for sources, users, schedules, and manual digest runs

## How it works

1. Newy reads source definitions from `data/sources.seed.json`.
2. For RSS sources, it parses feed entries directly.
3. For web-only sources, it opens pages in a browser when needed, extracts bounded navigation candidates, and asks Codex which actions to take next.
4. It validates article pages before saving them to SQLite.
5. It clusters and ranks recent articles, then asks Codex to generate a grounded digest.
6. It stores the digest and optionally sends it to WhatsApp via Twilio.

## Repository layout

```text
newy/
├── newy/                  # application package
│   ├── browser_fetcher.py
│   ├── cli.py
│   ├── config.py
│   ├── delivery.py
│   ├── feed_fetcher.py
│   ├── models.py
│   ├── navigation_agent.py
│   ├── page_extractors.py
│   ├── ranking.py
│   ├── services.py
│   ├── source_catalog.py
│   ├── storage.py
│   ├── summarizer.py
│   └── web.py
├── data/
│   ├── config.example.json
│   └── sources.seed.json
├── scripts/
│   └── install_browser_support.sh
├── tests/
├── pyproject.toml
└── README.md
```

## Prerequisites

- Python 3.11+
- `codex` installed and authenticated if you want Codex-based navigation/summarization
- Internet access for live ingestion

Optional:
- Playwright + Chromium for browser-rendered navigation
- Twilio WhatsApp credentials for real message delivery

## Setup

### Recommended setup

```bash
cd newy
./scripts/install_browser_support.sh
source .venv/bin/activate
```

This script creates a local virtual environment, installs the project with browser extras, installs Chromium for Playwright, and verifies the browser runtime.

### Manual setup

```bash
cd newy
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[browser]
playwright install chromium
```

If you do not need browser rendering, you can use:

```bash
pip install -e .
```

## Configuration

Copy the example config and create a local override:

```bash
cp data/config.example.json data/config.local.json
```

`data/config.local.json` is intentionally ignored by git.

### Key config sections

#### Codex

```json
"codex": {
  "enabled": true,
  "command": "codex"
}
```

#### Browser rendering

```json
"browser": {
  "enabled": true,
  "engine": "chromium",
  "headless": true,
  "timeout_seconds": 30,
  "wait_until": "networkidle"
}
```

#### Navigation tuning

```json
"max_navigation_actions_per_page": 6,
"max_navigation_retries_per_page": 2
```

#### Twilio

For local dry-run testing:

```json
"twilio": {
  "from_number": "whatsapp:+14155238886",
  "dry_run": true,
  "validate_signature": false
}
```

For real sending, set `dry_run` to `false` and export credentials:

```bash
export TWILIO_ACCOUNT_SID="your_sid"
export TWILIO_AUTH_TOKEN="your_token"
```

## Source configuration

Source definitions live in `data/sources.seed.json`.

Supported source types:
- `rss`
- `archive_page`
- `newsletter_archive`

Example archive/newsletter metadata:

```json
{
  "link_prefixes": ["/news/"],
  "exclude_contains": ["/video/", "/photos/"],
  "max_links": 8,
  "max_navigation_steps": 2,
  "max_navigation_actions": 6,
  "max_navigation_retries": 2,
  "use_browser": true,
  "allow_heuristic_fallback": true
}
```

## Local usage

### Initialize the database

```bash
python3 -m newy --config data/config.local.json init-db
```

### Seed a demo user

```bash
python3 -m newy --config data/config.local.json seed-demo
```

### Force one ingestion run

```bash
python3 -m newy --config data/config.local.json ingest --force
```

### Generate a digest manually

```bash
python3 -m newy --config data/config.local.json digest --user-id 1 --topic "US Iran conflict"
```

### Start the admin UI

```bash
python3 -m newy --config data/config.local.json serve-admin
```

If `admin_token` is set, open:

```text
http://127.0.0.1:8080/?token=YOUR_ADMIN_TOKEN
```

### Start the worker loop

```bash
python3 -m newy --config data/config.local.json worker
```

## WhatsApp via Twilio

Newy uses **Twilio WhatsApp** for delivery.

### Fastest testing path: Twilio Sandbox

1. Create a Twilio account.
2. Open the WhatsApp Sandbox in Twilio Console.
3. Join the sandbox from your phone by sending the displayed join code to the sandbox number.
4. Export credentials:

```bash
export TWILIO_ACCOUNT_SID="your_sid"
export TWILIO_AUTH_TOKEN="your_token"
```

5. Set in `data/config.local.json`:

```json
"twilio": {
  "from_number": "whatsapp:+14155238886",
  "dry_run": false,
  "validate_signature": false
}
```

6. Expose your local server publicly, for example with ngrok:

```bash
ngrok http 8080
```

7. In Twilio Sandbox settings, set the incoming webhook to:

```text
https://YOUR_PUBLIC_URL/webhooks/twilio
```

8. Start Newy admin + worker.
9. Send a message such as:

```text
digest Iran ceasefire
```

### Production path

For production use:
- use an approved Twilio WhatsApp sender
- set `dry_run` to `false`
- set `validate_signature` to `true`
- set `public_base_url` to your public HTTPS domain
- point Twilio inbound webhook to `/webhooks/twilio`

## Diagnostics

Newy stores source-run diagnostics in SQLite, including:
- source status
- visited pages
- attempted article URLs
- validated article URLs
- chosen navigation actions
- warnings/errors

This makes navigation failures easier to inspect during development.

## Tests

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Check syntax:

```bash
python3 -m py_compile newy/*.py
```

## Current limitations

This repo is suitable for pilot/internal workflows, but some production-hardening issues remain, especially:
- shared SQLite connection with threaded HTTP is only locally hardened, not ideal for high concurrency
- webhook/admin handlers still do synchronous work
- admin token uses query-string/header auth rather than a proper session flow

## License / publishing notes

Before publishing to GitHub, review:
- `data/sources.seed.json` for the source set you want public
- `data/config.example.json` to ensure no private values are present
- Twilio and Codex usage notes to match your intended public documentation
