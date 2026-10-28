"""Read-only HTTP delivery. The directory is rescanned for every request."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from urllib.parse import unquote, urlsplit

from .catalog import digest, snapshot


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, token=None):
        self.root, self.token = root, token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info("%s %s", self.client_address[0], fmt % args)

    def reply(self, status, body=b"", content_type="application/json", etag=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.server.token and not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.server.token
        ):
            self.reply(401, b'{"error":"Unauthorized"}')
            return
        route = unquote(urlsplit(self.path).path)
        if route == "/healthz":
            self.reply(200, b'{"status":"ok"}')
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
