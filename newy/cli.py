from __future__ import annotations

import argparse
import json

from .config import AppConfig
from .services import NewyApp
from .web import serve_admin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newy", description="Trusted news digests to WhatsApp")
    parser.add_argument("--config", help="Path to JSON config file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Initialize the database and bootstrap seed sources")
    sub.add_parser("serve-admin", help="Run the admin + webhook HTTP server")
    ingest = sub.add_parser("ingest", help="Poll due sources or force one pass")
    ingest.add_argument("--force", action="store_true")
    worker = sub.add_parser("worker", help="Run background worker")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("seed-demo", help="Seed a demo user and schedule")
    digest = sub.add_parser("digest", help="Queue and send a topic digest immediately")
    digest.add_argument("--user-id", type=int, required=True)
    digest.add_argument("--topic", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = AppConfig.load(args.config)
    app = NewyApp(config)

    if args.command == "init-db":
        print(json.dumps({"database": str(config.resolve_path(config.database_path)), "sources": len(app.store.list_sources())}, indent=2))
        return 0
    if args.command == "serve-admin":
        serve_admin(app)
        return 0
    if args.command == "ingest":
        print(json.dumps(app.ingest_due_sources(force=args.force), indent=2))
        return 0
    if args.command == "worker":
        app.run_worker(once=args.once)
        return 0
    if args.command == "seed-demo":
        print(json.dumps({"user_id": app.seed_demo_user()}, indent=2))
        return 0
    if args.command == "digest":
        app.queue_topic_digest(user_id=args.user_id, topic=args.topic)
        processed = app.process_jobs(limit=5)
        print(json.dumps({"processed_jobs": processed}, indent=2))
        return 0
    parser.error("unknown command")
    return 2
