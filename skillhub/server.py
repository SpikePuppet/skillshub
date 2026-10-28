"""Read-only HTTP delivery plus a small dashboard. The directory is rescanned for every request.

With a client config attached (local `start` or `ui`), the dashboard can also
edit that user's settings. Settings changes are accepted only from loopback
clients addressing the server by a loopback host name, and only with a custom
header that cross-site pages cannot send without a CORS preflight.
"""
import collections
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import mimetypes
from pathlib import Path
import re
import threading
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .catalog import MAX_BYTES, NAME, digest, snapshot
from .client import read_state, record_state, sync, wait_lock
from .library import Conflict, add_skills
from .projects import PROJECT_PATHS, read_config, read_projects, update_project

DASHBOARD = Path(__file__).with_name("web") / "index.html"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
QUIET_ROUTES = ("/", "/index.html", "/favicon.ico")
UPLOAD_BODY_LIMIT = 96 * 1024 * 1024  # 64 MiB of files, base64-encoded in JSON
CLIENT_LOG_TAIL = 256 * 1024
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CLIENT_LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)")


class LogBuffer(logging.Handler):
    """Keeps this process's recent log records for the dashboard's Logs panel."""

    def __init__(self, size=500):
        super().__init__(logging.INFO)
        self.records, self.seq, self.guard = collections.deque(maxlen=size), 0, threading.Lock()

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        level = "SUCCESS" if getattr(record, "success", False) else record.levelname
        http = getattr(record, "http", None)
        with self.guard:
            self.seq += 1
            self.records.append({"seq": self.seq, "time": record.created, "level": level,
                                 "message": message, "http": list(http) if http else None})

    def since(self, seq):
        with self.guard:
            return [r for r in self.records if r["seq"] > seq], self.seq


LOGS = LogBuffer()


def tail_client_log(path, limit=300):
    try:
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - CLIENT_LOG_TAIL))
            text = ANSI.sub("", stream.read().decode("utf-8", "replace"))
    except OSError:
        return []
    lines = text.splitlines()[1 if size > CLIENT_LOG_TAIL else 0:]
    entries = []
    for line in lines:
        match = CLIENT_LOG_LINE.match(line)
        if match:
            entries.append({"stamp": match[1], "level": match[2], "message": match[3]})
        elif line.strip() and entries:
            entries[-1]["message"] += "\n" + line  # traceback or wrapped output
        elif line.strip():
            entries.append({"stamp": None, "level": "INFO", "message": line})
    return entries[-limit:]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, token=None, config=None):
        self.root, self.token, self.config = root, token, config
        root_logger = logging.getLogger()
        if LOGS not in root_logger.handlers:
            root_logger.addHandler(LOGS)
            if root_logger.level > logging.INFO or root_logger.level == logging.NOTSET:
                root_logger.setLevel(logging.INFO)
        super().__init__(address, Handler)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        code = int(code) if str(code).isdigit() else 0
        route = urlsplit(self.path).path
        # The dashboard and watchers poll; unchanged checks would drown out real activity.
        if code == 304 or (code < 400 and (route in QUIET_ROUTES or route.startswith("/api/"))):
            return
        method = self.command or "-"
        logging.info("%s %s %s %s", self.client_address[0], method, route, code,
                     extra={"http": (method, route, code, self.client_address[0])})

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.client_address[0], fmt % args)

    def reply(self, status, body=b"", content_type="application/json", etag=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status, value):
        self.reply(status, json.dumps(value).encode())

    def authorized(self):
        return not self.server.token or hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.server.token)

    def local(self):
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False
        host = (self.headers.get("Host") or "").strip()
        host = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        return loopback and host.lower() in LOOPBACK_HOSTS and self.headers.get("X-SkillHub") == "1"

    def do_GET(self):
        route = unquote(urlsplit(self.path).path)
        if route in ("/", "/index.html"):
            self.reply(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            return
        if not self.authorized():
            self.reply(401, b'{"error":"Unauthorized"}')
            return
        if route == "/healthz":
            self.reply(200, b'{"status":"ok"}')
            return
        if route.startswith("/api/"):
            self.api("GET", route)
            return
        if self.server.root is None:
            self.reply(404, b'{"error":"Not found"}')
            return
        try:
            manifest, files, bundle = snapshot(self.server.root)
        except Exception as exc:
            logging.warning("Catalog invalid: %s", exc)
            self.reply(422, json.dumps({"error": str(exc)}).encode())
            return
        if route == "/v1/bundle.zip":
            etag = '"' + digest(bundle) + '"'
            if self.headers.get("If-None-Match") == etag:
                self.reply(304, etag=etag)
            else:
                self.reply(200, bundle, "application/zip", etag)
        elif route == "/v1/catalog":
            self.reply(200, json.dumps(manifest).encode())
        elif route == "/.well-known/skills/index.json":
            index = {"skills": [{**s, "files": list(s["files"])} for s in manifest["skills"]]}
            self.reply(200, json.dumps(index).encode())
        elif route.startswith("/.well-known/skills/") and route[len("/.well-known/skills/"):] in files:
            key = route[len("/.well-known/skills/"):]
            self.reply(200, files[key], mimetypes.guess_type(key)[0] or "application/octet-stream")
        else:
            self.reply(404, b'{"error":"Not found"}')

    def do_POST(self):
        self.mutate("POST")

    def do_PUT(self):
        self.mutate("PUT")

    def do_DELETE(self):
        self.mutate("DELETE")

    def mutate(self, method):
        route = unquote(urlsplit(self.path).path)
        if not route.startswith("/api/"):
            self.reply(405, b'{"error":"Method not allowed"}')
        elif not self.authorized():
            self.reply(401, b'{"error":"Unauthorized"}')
        else:
            self.api(method, route)

    def body(self, limit=64 * 1024):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ApiError(415, "Expected a JSON request body")
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ApiError(413, "Request body too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ApiError(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise ApiError(400, "Expected a JSON object")
        return value

    def api(self, method, route):
        try:
            if method == "GET" and route == "/api/overview":
                local = self.local()
                self.json(200, {"source": self.source(), **self.skills(), "client": self.client(),
                                "can_upload": local and self.server.root is not None, "can_view_logs": local})
            elif method == "GET" and route == "/api/logs":
                self.json(200, self.logs())
            elif method == "POST" and route == "/api/library":
                root = self.library_root()
                body = self.body(UPLOAD_BODY_LIMIT)
                try:
                    names = add_skills(root, body.get("files"), body.get("replace") is True)
                except Conflict as exc:
                    self.json(409, {"error": str(exc), "names": exc.names})
                    return
                logging.info("Added %s from the dashboard: %s", "skill" if len(names) == 1 else "skills",
                             ", ".join(names), extra={"success": True})
                self.json(200, {"added": names})
            elif method == "GET" and route.startswith("/api/skills/"):
                self.reply(200, self.skill_file(route[len("/api/skills/"):]), "text/plain; charset=utf-8")
            elif method == "PUT" and route == "/api/settings":
                from .setup import update_interval
                config, interval = self.settings_config(), self.body().get("interval")
                if isinstance(interval, bool) or not isinstance(interval, (int, float)):
                    raise ApiError(400, "Interval must be a number of seconds")
                update_interval(config, interval)
                self.json(200, self.client())
            elif method in ("POST", "DELETE") and route == "/api/projects":
                config, body = self.settings_config(), self.body()
                if not isinstance(body.get("path"), str) or not body["path"].strip():
                    raise ApiError(400, "Enter a directory path")
                choices = (body.get("harnesses") or ["all"]) if method == "POST" else None
                update_project(config, Path(body["path"].strip()), choices)
                self.json(200, self.client())
            elif method == "POST" and route == "/api/sync":
                config = self.settings_config()
                try:
                    with wait_lock(config.parent / "sync.lock"):
                        _, count = sync(read_config(config))
                except Exception as exc:
                    record_state(config, error=str(exc))
                    raise
                record_state(config, count)
                self.json(200, {"changed": count, "client": self.client()})
            else:
                raise ApiError(404, "Not found")
        except ApiError as exc:
            self.json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            self.json(400, {"error": str(exc)})
        except Exception as exc:
            logging.exception("Dashboard request failed")
            self.json(500, {"error": str(exc)})

    def library_root(self):
        if self.server.root is None:
            raise ApiError(404, "This dashboard is not serving a skills directory; add skills on the server")
        if not self.local():
            raise ApiError(403, "Skills can only be added from the server's own computer")
        return self.server.root

    def logs(self):
        if not self.local():
            raise ApiError(403, "Logs are only available on this computer")
        try:
            since = int(parse_qs(urlsplit(self.path).query).get("since", ["0"])[0])
        except ValueError:
            since = 0
        entries, last = LOGS.since(since)
        config = self.server.config
        client = None
        if config and config.exists():
            path = config.parent / "client.log"
            client = {"path": str(path), "entries": tail_client_log(path)}
        return {"server": {"entries": entries, "last": last}, "client": client}

    def settings_config(self):
        if not self.server.config:
            raise ApiError(404, "This server has no client settings")
        if not self.local():
            raise ApiError(403, "Settings can only be changed from this computer")
        if not self.server.config.exists():
            raise ApiError(409, "This computer is not set up yet; run 'skillhub setup' first")
        return self.server.config

    def source(self):
        if self.server.root is not None:
            return {"kind": "local", "location": str(self.server.root)}
        config = self.server.config
        url = json.loads(config.read_text())["url"] if config and config.exists() else None
        return {"kind": "remote", "location": url}

    def skills(self):
        try:
            if self.server.root is not None:
                manifest, files, _ = snapshot(self.server.root)
                sizes = {key: len(data) for key, data in files.items()}
            else:
                manifest, sizes = json.loads(self.remote("/v1/catalog")), {}
        except Exception as exc:
            return {"skills": [], "error": str(exc)}
        skills = []
        for skill in manifest["skills"]:
            names = sorted(skill["files"])
            size = sum(sizes.get(f"{skill['name']}/{name}", 0) for name in names)
            skills.append({"name": skill["name"], "description": skill["description"],
                           "version": skill["version"][:12], "files": names, "bytes": size or None})
        return {"skills": skills, "error": None}

    def skill_file(self, name):
        if not NAME.fullmatch(name):
            raise ApiError(404, "Unknown skill")
        key = f"{name}/SKILL.md"
        if self.server.root is not None:
            try:
                return snapshot(self.server.root)[1][key]
            except KeyError:
                raise ApiError(404, "Unknown skill") from None
        return self.remote("/.well-known/skills/" + quote(key))

    def remote(self, route):
        config = self.server.config
        if not config or not config.exists():
            raise ApiError(409, "This computer is not set up yet; run 'skillhub setup' first")
        value = json.loads(config.read_text())
        headers = {"User-Agent": "SkillHub/0.1"}
        if value.get("token"):
            headers["Authorization"] = "Bearer " + value["token"]
        request = urllib.request.Request(value["url"].rstrip("/") + route, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read(MAX_BYTES)

    def client(self):
        config = self.server.config
        if not config or not self.local():
            return None
        if not config.exists():
            return {"configured": False, "config": str(config)}
        from .setup import watcher_running
        value = json.loads(config.read_text())
        try:
            projects, projects_error = read_projects(config), None
        except ValueError as exc:
            projects, projects_error = [], str(exc)
        return {
            "configured": True, "config": str(config), "url": value["url"],
            "interval": value.get("interval", 5), "targets": value["targets"],
            "projects": [{**p, "exists": Path(p["path"]).is_dir()} for p in projects],
            "projects_error": projects_error, "harnesses": list(PROJECT_PATHS),
            "running": watcher_running(config), "state": read_state(config),
            "log": str(config.parent / "client.log"),
        }
