from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .delivery import validate_twilio_signature
from .models import Source, User
from .services import NewyApp


class AdminServer(ThreadingHTTPServer):
    def __init__(self, server_address, request_handler_class, app: NewyApp):
        super().__init__(server_address, request_handler_class)
        self.app = app


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._write_text(HTTPStatus.OK, "ok")
            return
        if not self._authorized(parsed.query):
            self._write_text(HTTPStatus.UNAUTHORIZED, "missing or invalid admin token")
            return
        if parsed.path in {"/", "/dashboard"}:
            self._write_html(HTTPStatus.OK, self._render_dashboard(parsed.query))
            return
        self._write_text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode()
        params = {key: values[-1] for key, values in parse_qs(raw_body).items()}
        if parsed.path == "/webhooks/twilio":
            self._handle_twilio_webhook(parsed, params)
            return
        token_value = parse_qs(parsed.query).get("token", [""])[0]
        if not self._authorized(raw_body, parsed.query):
            self._write_text(HTTPStatus.UNAUTHORIZED, "missing or invalid admin token")
            return
        try:
            if parsed.path == "/sources":
                metadata = json.loads(params.get("metadata_json", "{}") or "{}")
                self.server.app.store.upsert_source(
                    Source(
                        slug=params["slug"],
                        name=params["name"],
                        feed_url=params["feed_url"],
                        base_url=params.get("base_url", ""),
                        region=params.get("region", "global"),
                        language=params.get("language", "en"),
                        trust_tier=int(params.get("trust_tier", 3)),
                        source_type=params.get("source_type", "rss"),
                        active=params.get("active", "1") == "1",
                        poll_interval_minutes=int(params.get("poll_interval_minutes", 30)),
                        fallback_allowed=params.get("fallback_allowed", "0") == "1",
                        metadata=metadata,
                    )
                )
                self._redirect("/", token_value)
                return
            if parsed.path == "/users":
                user_id = self.server.app.store.upsert_user(
                    User(
                        id=None,
                        name=params["name"],
                        phone_number=params["phone_number"],
                        preferred_language=params.get("preferred_language", "bilingual"),
                        topics=[item.strip() for item in params.get("topics", "").split(",") if item.strip()],
                        regions=[item.strip() for item in params.get("regions", "").split(",") if item.strip()],
                        active=params.get("active", "1") == "1",
                    )
                )
                schedule_time = params.get("schedule_local_time", "").strip()
                if schedule_time:
                    self.server.app.store.upsert_schedule(
                        user_id,
                        schedule_time,
                        params.get("timezone", self.server.app.config.timezone_default),
                        enabled=True,
                    )
                self._redirect("/", token_value)
                return
            if parsed.path == "/actions/ingest":
                self.server.app.ingest_due_sources(force=True)
                self._redirect("/", token_value)
                return
            if parsed.path == "/actions/digest":
                self.server.app.queue_topic_digest(user_id=int(params["user_id"]), topic=params["topic"])
                self.server.app.process_jobs(limit=5)
                self._redirect("/", token_value)
                return
        except Exception as exc:
            self._write_text(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._write_text(HTTPStatus.NOT_FOUND, "not found")

    def _handle_twilio_webhook(self, parsed, params: dict[str, str]) -> None:
        config = self.server.app.config
        if config.twilio.validate_signature:
            forwarded_proto = self.headers.get("X-Forwarded-Proto", "http")
            forwarded_host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", ""))
            base_url = config.public_base_url.rstrip("/")
            if base_url:
                url = base_url + parsed.path
            else:
                url = f"{forwarded_proto}://{forwarded_host}{parsed.path}"
            signature = self.headers.get("X-Twilio-Signature", "")
            if not validate_twilio_signature(url, params, signature, config.twilio.auth_token):
                self._write_text(HTTPStatus.UNAUTHORIZED, "invalid twilio signature")
                return

        phone_number = params.get("From", "")
        message = (params.get("Body", "") or "").strip()
        user = self.server.app.store.find_user_by_phone(phone_number)
        if not user:
            self._write_twiml("Unknown user. Ask an operator to register this WhatsApp number.")
            return
        normalized = message.lower()
        if normalized.startswith("digest "):
            topic = message[7:].strip()
        elif normalized.startswith("today "):
            topic = message[6:].strip()
        else:
            topic = message.strip()
        if not topic:
            self._write_twiml("Send any topic text, or use: digest <topic>.")
            return

        self.server.app.queue_topic_digest(user_id=int(user["id"]), topic=topic)
        self.server.app.process_jobs(limit=5)
        self._write_twiml(f"Received. Preparing digest for: {topic}")

    def _authorized(self, raw_form: str, raw_query: str = "") -> bool:
        token = self.server.app.config.admin_token
        if not token:
            return True
        form = parse_qs(raw_form)
        query = parse_qs(raw_query)
        if form.get("token", [""])[0] == token or query.get("token", [""])[0] == token:
            return True
        if self.headers.get("X-Admin-Token", "") == token:
            return True
        return False

    def _render_dashboard(self, query: str) -> str:
        token = parse_qs(query).get("token", [""])[0]
        token_suffix = f"?{urlencode({'token': token})}" if token else ""
        sources = self.server.app.store.list_sources()
        users = self.server.app.store.list_users()
        deliveries = self.server.app.store.list_deliveries(20)

        def esc(value: object) -> str:
            return html.escape(str(value))

        source_rows = "".join(
            f"<tr><td>{esc(row['slug'])}</td><td>{esc(row['name'])}</td><td>{esc(row['source_type'])}</td><td>{esc(row['region'])}</td><td>{esc(row['language'])}</td><td>{esc(row['trust_tier'])}</td><td>{esc(row['last_error'] or '')}</td></tr>"
            for row in sources
        ) or '<tr><td colspan="7">No sources yet</td></tr>'
        user_rows = "".join(
            f"<tr><td>{esc(row['id'])}</td><td>{esc(row['name'])}</td><td>{esc(row['phone_number'])}</td><td>{esc(row['preferred_language'])}</td><td>{esc(row['topics_json'])}</td></tr>"
            for row in users
        ) or '<tr><td colspan="5">No users yet</td></tr>'
        delivery_rows = "".join(
            f"<tr><td>{esc(row['created_at'])}</td><td>{esc(row['destination'])}</td><td>{esc(row['status'])}</td><td>{esc(row['error'] or row['external_id'] or '')}</td></tr>"
            for row in deliveries
        ) or '<tr><td colspan="4">No deliveries yet</td></tr>'

        return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <title>Newy Admin</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
    td, th {{ border: 1px solid #ddd; padding: 0.5rem; text-align: left; vertical-align: top; }}
    form {{ margin-bottom: 2rem; padding: 1rem; border: 1px solid #ddd; }}
    input, textarea, select {{ margin: 0.25rem; min-width: 14rem; }}
    textarea {{ width: 100%; min-height: 5rem; }}
  </style>
</head>
<body>
  <h1>Newy Admin</h1>
  <p><a href=\"/healthz\">Health</a></p>
  <form method=\"post\" action=\"/actions/ingest{token_suffix}\">
    <button type=\"submit\">Force ingest now</button>
  </form>

  <h2>Sources</h2>
  <form method=\"post\" action=\"/sources{token_suffix}\">
    <input name=\"slug\" placeholder=\"slug\" required>
    <input name=\"name\" placeholder=\"Display name\" required>
    <input name=\"feed_url\" placeholder=\"RSS/Archive URL\" required>
    <input name=\"base_url\" placeholder=\"Base URL\">
    <input name=\"region\" placeholder=\"Region\" value=\"global\">
    <input name=\"language\" placeholder=\"Language\" value=\"en\">
    <select name=\"source_type\">
      <option value=\"rss\">rss</option>
      <option value=\"archive_page\">archive_page</option>
      <option value=\"newsletter_archive\">newsletter_archive</option>
    </select>
    <input name=\"trust_tier\" type=\"number\" min=\"1\" max=\"5\" value=\"4\">
    <input name=\"poll_interval_minutes\" type=\"number\" min=\"5\" value=\"30\">
    <label><input name=\"fallback_allowed\" type=\"checkbox\" value=\"1\">Fallback</label>
    <textarea name="metadata_json" placeholder='{{"link_prefixes": ["/news/"], "exclude_contains": ["/video/"]}}'></textarea>
    <button type=\"submit\">Save source</button>
  </form>
  <table>
    <tr><th>Slug</th><th>Name</th><th>Type</th><th>Region</th><th>Lang</th><th>Trust</th><th>Last error</th></tr>
    {source_rows}
  </table>

  <h2>Users</h2>
  <form method=\"post\" action=\"/users{token_suffix}\">
    <input name=\"name\" placeholder=\"Name\" required>
    <input name=\"phone_number\" placeholder=\"whatsapp:+123...\" required>
    <input name=\"preferred_language\" placeholder=\"english|arabic|bilingual\" value=\"bilingual\">
    <input name=\"topics\" placeholder=\"topic1, topic2\">
    <input name=\"regions\" placeholder=\"global, middle-east\">
    <input name=\"schedule_local_time\" placeholder=\"08:00\">
    <input name=\"timezone\" placeholder=\"UTC\" value=\"{esc(self.server.app.config.timezone_default)}\">
    <button type=\"submit\">Save user</button>
  </form>
  <table>
    <tr><th>ID</th><th>Name</th><th>Phone</th><th>Language</th><th>Topics</th></tr>
    {user_rows}
  </table>

  <h2>Run on-demand digest</h2>
  <form method=\"post\" action=\"/actions/digest{token_suffix}\">
    <input name=\"user_id\" placeholder=\"User ID\" required>
    <input name=\"topic\" placeholder=\"e.g. US-Iran conflict\" required>
    <button type=\"submit\">Generate + send</button>
  </form>

  <h2>Deliveries</h2>
  <table>
    <tr><th>Time</th><th>Destination</th><th>Status</th><th>Result</th></tr>
    {delivery_rows}
  </table>
</body>
</html>"""

    def _redirect(self, path: str, token: str = "") -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        location = f"{path}?{urlencode({'token': token})}" if token else path
        self.send_header("Location", location)
        self.end_headers()

    def _write_text(self, status: HTTPStatus, text: str) -> None:
        encoded = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_html(self, status: HTTPStatus, html_body: str) -> None:
        encoded = html_body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_twiml(self, message: str) -> None:
        body = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{html.escape(message)}</Message></Response>'
        encoded = body.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve_admin(app: NewyApp) -> None:
    server = AdminServer((app.config.admin_host, app.config.admin_port), AdminHandler, app)
    print(f"Newy admin listening on http://{app.config.admin_host}:{app.config.admin_port}")
    server.serve_forever()
