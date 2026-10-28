"""Terminal presentation: colour, symbols, and a log formatter.

Styling is used only for interactive terminals. Redirected output and log files
stay plain so they remain easy to grep and parse. NO_COLOR and FORCE_COLOR are
honoured.
"""
import logging
import os
import sys
import time

CODES = {
    "reset": "0", "bold": "1", "dim": "2", "italic": "3",
    "red": "38;5;203", "green": "38;5;114", "yellow": "38;5;221", "blue": "38;5;111",
    "magenta": "38;5;176", "cyan": "38;5;117", "grey": "38;5;244", "accent": "38;5;209",
}
SYMBOLS = {"info": "●", "ok": "✓", "warn": "▲", "error": "✗", "arrow": "➜", "brand": "◆", "dot": "·"}
ASCII = {"info": "*", "ok": "+", "warn": "!", "error": "x", "arrow": ">", "brand": "#", "dot": "-"}


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def supports_color(stream):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


def supports_unicode(stream):
    try:
        "".join(SYMBOLS.values()).encode(getattr(stream, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Style:
    def __init__(self, stream):
        self.stream = stream
        self.color = supports_color(stream)
        self.symbols = SYMBOLS if supports_unicode(stream) else ASCII
        if self.color:
            _enable_windows_ansi()

    def __call__(self, text, *styles):
        if not self.color or not styles:
            return str(text)
        return f"\033[{';'.join(CODES[s] for s in styles)}m{text}\033[0m"

    def symbol(self, name):
        return self.symbols[name]


out = Style(sys.stdout)
err = Style(sys.stderr)


def _line(style, stream, kind, color, message):
    print(f"  {style(style.symbol(kind), color, 'bold')} {message}", file=stream, flush=True)


def info(message):
    _line(out, sys.stdout, "info", "blue", message)


def success(message):
    _line(out, sys.stdout, "ok", "green", message)


def warn(message):
    _line(out, sys.stdout, "warn", "yellow", message)


def error(message):
    _line(err, sys.stderr, "error", "red", err(message, "red"))


def heading(title, subtitle=""):
    brand = out(out.symbol("brand"), "accent", "bold")
    tail = f"  {out(subtitle, 'grey')}" if subtitle else ""
    print(f"\n  {brand} {out(title, 'bold')}{tail}\n", flush=True)


def fields(rows):
    """Aligned label/value rows, Vite-style. Rows are (label, value) pairs."""
    width = max((len(label) for label, _ in rows), default=0)
    for label, value in rows:
        print(f"  {out(out.symbol('arrow'), 'accent')}  {out(label.ljust(width), 'bold')}   {value}", flush=True)


def link(url):
    return out(url, "cyan", "bold")


def muted(text):
    return out(text, "grey")


def blank():
    print(flush=True)


class Formatter(logging.Formatter):
    """Coloured, compact log lines for terminals; plain timestamped lines elsewhere."""
    LEVELS = {
        logging.DEBUG: ("dot", "grey"),
        logging.INFO: ("info", "blue"),
        logging.WARNING: ("warn", "yellow"),
        logging.ERROR: ("error", "red"),
        logging.CRITICAL: ("error", "red"),
    }

    def __init__(self, stream):
        super().__init__()
        self.style = Style(stream)

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        if not self.style.color:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
            return f"{stamp} {record.levelname} {message}"
        s = self.style
        kind, color = self.LEVELS.get(record.levelno, ("info", "blue"))
        if getattr(record, "success", False):
            kind, color = "ok", "green"
        if getattr(record, "http", None):
            message = self.http(record.http)
        elif record.levelno >= logging.ERROR:
            message = s(message, "red")
        stamp = s(time.strftime("%H:%M:%S", time.localtime(record.created)), "grey")
        return f"  {stamp}  {s(s.symbol(kind), color, 'bold')}  {message}"

    def http(self, request):
        s = self.style
        method, path, status, client = request
        status_color = "green" if status < 300 else "cyan" if status < 400 else "yellow" if status < 500 else "red"
        return (f"{s(method.ljust(4), 'magenta', 'bold')} {path}  "
                f"{s(status, status_color, 'bold')}  {s(client, 'grey')}")


def configure_logging():
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(Formatter(sys.stderr))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
