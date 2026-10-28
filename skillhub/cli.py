import argparse
import json
import logging
from pathlib import Path
import os
import sys
import threading
import time

from .catalog import snapshot
from .client import lock, sync
from .server import Server
from .setup import configure, default_config, startup_path, stop_background, start_background


def watch(config):
    with lock(config.parent / "watch.lock"):
        etag, previous = None, None
        stop = config.parent / "stop"
        logging.info("Watching %s", config)
        while not stop.exists():
            interval = 5
            try:
                value = json.loads(config.read_text())
                interval = max(1, float(value["interval"]))
                if value != previous:
                    etag = None
                with lock(config.parent / "sync.lock"):
                    etag, count = sync(value, etag)
                previous = value
                if count:
                    logging.info("Updated %s skill folders", count)
            except Exception as exc:
                logging.error("Sync failed; installed skills retained: %s", exc)
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not stop.exists():
                time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description="Serve a shared skill library to Cursor and OpenCode")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "start"):
        p = sub.add_parser(command, help="Serve skills" + (" and offer local setup" if command == "start" else ""))
        p.add_argument("--skills", type=Path, default=Path("skills"))
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8765)
        if command == "start":
            p.add_argument("--config", type=Path, default=default_config())
    p = sub.add_parser("setup", help="Connect this user's harnesses to a running service")
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--harness", choices=["cursor", "opencode", "both"])
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--config", type=Path, default=default_config())
    for command in ("sync", "watch", "stop", "status"):
        p = sub.add_parser(command)
        p.add_argument("--config", type=Path, default=default_config())
        if command == "stop":
            p.add_argument("--disable-autostart", action="store_true")
    p = sub.add_parser("validate")
    p.add_argument("--skills", type=Path, default=Path("skills"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(args, "config"):
        args.config = args.config.expanduser().absolute()
    try:
        if args.command in ("start", "serve"):
            manifest, _, _ = snapshot(args.skills)
            server = Server((args.host, args.port), args.skills.absolute(), os.environ.get("SKILLHUB_TOKEN"))
            port = server.server_address[1]
            print(f"Serving {len(manifest['skills'])} skills from {args.skills.absolute()} at http://{args.host}:{port}", flush=True)
            if args.command == "start":
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    if not args.config.exists():
                        host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
                        configure(args.config, f"http://{host}:{port}", None, 5)
                    else:
                        start_background(args.config)
                    while worker.is_alive():
                        worker.join(0.5)
                finally:
                    server.shutdown()
                    server.server_close()
            else:
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
        elif args.command == "setup":
            configure(args.config, args.url, args.harness, args.interval, not args.no_autostart, not args.no_start)
        elif args.command == "sync":
            with lock(args.config.parent / "sync.lock"):
                _, count = sync(json.loads(args.config.read_text()))
                print(f"Updated {count} skill folders")
        elif args.command == "watch":
            watch(args.config)
        elif args.command == "stop":
            if args.disable_autostart:
                startup_path().unlink(missing_ok=True)
            stop_background(args.config)
            print("Background sync stopped. Installed skills remain available.")
        elif args.command == "status":
            value = json.loads(args.config.read_text())
            try:
                with lock(args.config.parent / "watch.lock"):
                    running = False
            except RuntimeError:
                running = True
            print(json.dumps({"url": value["url"], "targets": value["targets"], "running": running,
                              "log": str(args.config.parent / "client.log")}, indent=2))
        elif args.command == "validate":
            manifest, _, _ = snapshot(args.skills)
            print(f"Valid: {len(manifest['skills'])} skills")
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"skillhub: {exc}", file=sys.stderr)
        raise SystemExit(1)
