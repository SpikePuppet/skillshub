import argparse
import json
import logging
from pathlib import Path
import os
import sys
import threading
import time

from . import __version__, console
from .catalog import snapshot
from .client import SKILLS_UPDATED_NOTICE, lock, read_state, record_state, sync
from .server import Server
from .projects import read_config, read_projects, projects_path, settings_signature, update_project
from .setup import (HARNESS_CHOICES, configure, default_config, startup_path, stop_background,
                    start_background, watcher_running)


def watch(config):
    with lock(config.parent / "watch.lock"):
        etag, previous = None, None
        stop = config.parent / "stop"
        logging.info("Watching %s", config)
        while not stop.exists():
            interval = 5
            signature = settings_signature(config)
            try:
                with lock(config.parent / "sync.lock"):
                    value = read_config(config)
                    interval = max(1, float(value["interval"]))
                    if value != previous:
                        etag = None
                    etag, count = sync(value, etag)
                previous = value
                record_state(config, count)
                if count:
                    logging.info("Updated %s skill folders", count, extra={"success": True})
                    logging.info(SKILLS_UPDATED_NOTICE)
            except Exception as exc:
                record_state(config, error=str(exc))
                logging.error("Sync failed; installed skills retained: %s", exc)
            # Wake early when settings change so a new interval or project applies at once.
            deadline = time.monotonic() + interval
            while (time.monotonic() < deadline and not stop.exists()
                   and settings_signature(config) == signature):
                time.sleep(0.2)


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def ago(timestamp):
    if not timestamp:
        return "never"
    seconds = max(0, int(time.time() - timestamp))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def wants_json(args):
    return getattr(args, "json", False) or not sys.stdout.isatty()


def serve(args):
    manifest, _, _ = snapshot(args.skills)
    config = args.config if args.command == "start" else None
    server = Server((args.host, args.port), args.skills.absolute(), os.environ.get("SKILLHUB_TOKEN"), config)
    port = server.server_address[1]
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{port}"
    console.heading("SkillHub", f"v{__version__}")
    console.fields([
        ("Dashboard", console.link(url + "/")),
        ("Skills", f"{plural(len(manifest['skills']), 'skill')} {console.muted('from ' + str(args.skills.absolute()))}"),
        ("Network", f"listening on {args.host}:{port}" + (console.muted(" · loopback only") if args.host in ("127.0.0.1", "localhost") else "")),
        ("Auth", "bearer token required" if server.token else console.muted("open (set SKILLHUB_TOKEN to require a token)")),
    ])
    console.blank()
    if args.command == "start":
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            if not args.config.exists():
                configure(args.config, url, None, 5)
            else:
                start_background(args.config)
                console.success("Background sync running " + console.muted(f"· log {args.config.parent / 'client.log'}"))
            console.info(f"Press {console.out('Ctrl+C', 'bold')} to stop the server\n")
            while worker.is_alive():
                worker.join(0.5)
        finally:
            server.shutdown()
            server.server_close()
    else:
        console.info(f"Press {console.out('Ctrl+C', 'bold')} to stop the server\n")
        try:
            server.serve_forever()
        finally:
            server.server_close()


def ui(args):
    if not args.config.exists():
        raise RuntimeError("This computer is not set up yet; run 'skillhub setup' first")
    server = Server(("127.0.0.1", args.port), None, None, args.config)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    console.heading("SkillHub dashboard", f"v{__version__}")
    console.fields([("Dashboard", console.link(url)),
                    ("Service", console.muted(json.loads(args.config.read_text())["url"]))])
    console.blank()
    console.info(f"Press {console.out('Ctrl+C', 'bold')} to close the dashboard\n")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def show_status(config):
    value = read_config(config)
    state = read_state(config)
    projects = read_projects(config)
    running = watcher_running(config)
    console.heading("SkillHub status")
    sync_line = (console.out("● running", "green", "bold") if running else console.out("○ stopped", "grey", "bold"))
    sync_line += console.muted(f"  every {float(value.get('interval', 5)):g}s")
    last = (console.out("failed " + ago(state.get("checked_at")), "red") if state.get("error")
            else ago(state.get("synced_at")))
    console.fields([
        ("Service", console.link(value["url"])),
        ("Sync", sync_line),
        ("Last sync", last),
        ("Log", console.muted(str(config.parent / "client.log"))),
    ])
    if state.get("error"):
        console.blank()
        console.error(state["error"])
    console.blank()
    print(f"  {console.out('Destinations', 'bold')}  {console.muted(str(len(value['targets'])))}")
    for target in value["targets"]:
        print(f"    {console.muted(console.out.symbol('dot'))} {target}")
    console.blank()
    show_projects(projects)


def show_projects(projects):
    print(f"  {console.out('Projects', 'bold')}  {console.muted(str(len(projects)))}")
    if not projects:
        print(f"    {console.muted('None yet — add one with: skillhub projects add PATH')}")
    for entry in projects:
        tags = " ".join(console.out(h, "cyan") for h in entry["harnesses"])
        print(f"    {console.muted(console.out.symbol('dot'))} {entry['path']}  {tags}")
    console.blank()


def report_sync(count):
    console.success(f"Updated {plural(count, 'skill folder')}" if count else "Already up to date")
    if count:
        console.warn(SKILLS_UPDATED_NOTICE)


def main():
    parser = argparse.ArgumentParser(description="Serve a shared skill library to Cursor, OpenCode, Codex, and Claude Code")
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
    p.add_argument("--harness", choices=HARNESS_CHOICES, action="append",
                   help="Repeat to select multiple harnesses; both = Cursor + OpenCode; all = all four")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--config", type=Path, default=default_config())
    p = sub.add_parser("ui", help="Open a local dashboard for this computer's skills and settings")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-open", action="store_true", help="Do not open a browser")
    p.add_argument("--config", type=Path, default=default_config())
    p = sub.add_parser("projects", help="Manage the project sync opt-in list")
    project_commands = p.add_subparsers(dest="project_command", required=True)
    for command in ("add", "remove", "list"):
        p = project_commands.add_parser(command)
        p.add_argument("--config", type=Path, default=default_config())
        if command != "list":
            p.add_argument("path", type=Path)
        if command == "add":
            p.add_argument("--harness", choices=HARNESS_CHOICES, action="append",
                           help="Repeat to select harnesses; defaults to all four")
        if command == "list":
            p.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    for command in ("sync", "watch", "stop", "status"):
        p = sub.add_parser(command)
        p.add_argument("--config", type=Path, default=default_config())
        if command == "stop":
            p.add_argument("--disable-autostart", action="store_true")
        if command == "status":
            p.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p = sub.add_parser("validate")
    p.add_argument("--skills", type=Path, default=Path("skills"))
    args = parser.parse_args()
    console.configure_logging()
    if hasattr(args, "config"):
        args.config = args.config.expanduser().absolute()
    try:
        if args.command in ("start", "serve"):
            serve(args)
        elif args.command == "ui":
            ui(args)
        elif args.command == "setup":
            configure(args.config, args.url, args.harness, args.interval, not args.no_autostart, not args.no_start)
        elif args.command == "sync":
            with lock(args.config.parent / "sync.lock"):
                try:
                    _, count = sync(read_config(args.config))
                except Exception as exc:
                    record_state(args.config, error=str(exc))
                    raise
                record_state(args.config, count)
            report_sync(count)
        elif args.command == "projects":
            if args.project_command == "list":
                projects = read_projects(args.config)
                if wants_json(args):
                    print(json.dumps(projects, indent=2))
                else:
                    console.blank()
                    show_projects(projects)
            else:
                choices = (args.harness or ["all"]) if args.project_command == "add" else None
                update_project(args.config, args.path, choices)
                if choices is not None:
                    console.success(f"Added {args.path.expanduser().resolve()}")
                    console.info("The running watcher picks it up right away; run "
                                 + console.out("skillhub sync", "bold") + " to sync in the foreground.")
                else:
                    console.success(f"Removed {args.path.expanduser().resolve()}")
                    console.info("Project updates stopped. Installed skills remain available.")
                print(f"    {console.muted(str(projects_path(args.config)))}")
        elif args.command == "watch":
            watch(args.config)
        elif args.command == "stop":
            if args.disable_autostart:
                startup_path().unlink(missing_ok=True)
            stop_background(args.config)
            console.success("Background sync stopped. Installed skills remain available.")
        elif args.command == "status":
            if wants_json(args):
                value = read_config(args.config)
                print(json.dumps({"url": value["url"], "targets": value["targets"],
                                  "running": watcher_running(args.config),
                                  "interval": value.get("interval", 5),
                                  "projects": read_projects(args.config),
                                  "projects_file": str(projects_path(args.config)),
                                  "state": read_state(args.config),
                                  "log": str(args.config.parent / "client.log")}, indent=2))
            else:
                show_status(args.config)
        elif args.command == "validate":
            manifest, _, _ = snapshot(args.skills)
            console.success(f"Valid: {plural(len(manifest['skills']), 'skill')}")
    except KeyboardInterrupt:
        console.blank()
        console.info("Stopped")
    except Exception as exc:
        console.error(str(exc))
        raise SystemExit(1)
