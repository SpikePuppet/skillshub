"""Per-user setup and login startup files; no administrator privileges needed."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from .client import SKILLS_UPDATED_NOTICE, atomic_json, lock, sync
from .projects import HARNESS_CHOICES, harnesses, with_projects


def default_config():
    return Path.home() / ".config" / "skillhub" / "client.json"


def targets(choice, home=None):
    home = Path(home) if home else Path.home()
    selected = harnesses(choice)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    paths = {
        "cursor": home / ".cursor" / "skills",
        "opencode": config_home / "opencode" / "skills",
        "codex": home / ".agents" / "skills",
        "claude-code": home / ".claude" / "skills",
    }
    return list(dict.fromkeys(str(path) for name, path in paths.items() if name in selected))


def startup_path():
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/local.skillhub.client.plist"
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/skillhub.cmd"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "autostart/skillhub.desktop"


def enable_startup(config):
    path = startup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "skillhub", "watch", "--config", str(config)]
    if sys.platform == "darwin":
        path.write_bytes(plistlib.dumps({
            "Label": "local.skillhub.client", "ProgramArguments": args,
            "RunAtLoad": True,
            "StandardOutPath": str(config.parent / "client.log"),
            "StandardErrorPath": str(config.parent / "client.log"),
        }))
    elif os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            args[0] = str(pythonw)
        path.write_text('@echo off\n' + subprocess.list2cmdline(args) + '\n')
    else:
        # Desktop Entry uses double-quote escaping, not shell quoting.
        def quote(arg):
            return '"' + arg.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
        path.write_text('[Desktop Entry]\nType=Application\nName=SkillHub\nExec=' + ' '.join(map(quote, args)) + '\nX-GNOME-Autostart-enabled=true\n')
    return path


def start_background(config):
    stop = config.parent / "stop"
    if stop.exists():
        raise RuntimeError("A stop request is pending. Run 'skillhub stop' before starting again.")
    try:
        with lock(config.parent / "watch.lock"):
            pass
    except RuntimeError:
        return
    with (config.parent / "client.log").open("ab") as log:
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        subprocess.Popen([sys.executable, "-m", "skillhub", "watch", "--config", str(config)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)


def stop_background(config):
    config.parent.mkdir(parents=True, exist_ok=True)
    marker = config.parent / "stop"
    marker.touch()
    for _ in range(250):
        try:
            with lock(config.parent / "watch.lock"):
                marker.unlink(missing_ok=True)
                return
        except RuntimeError:
            time.sleep(0.1)
    raise RuntimeError("Sync is still stopping; try again after the current download finishes")


def configure(config, url, choice, interval, autostart=True, start=True):
    if not choice:
        answer = input("Set up [1] Cursor, [2] OpenCode, [3] Cursor + OpenCode, "
                       "[4] Codex, [5] Claude Code, [6] all, [0] server only: ").strip()
        if answer == "0":
            return
        choice = {"1": "cursor", "2": "opencode", "3": "both",
                  "4": "codex", "5": "claude-code", "6": "all"}.get(answer)
        if not choice:
            raise ValueError("Choose 0, 1, 2, 3, 4, 5, or 6")
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Use an HTTP(S) service URL without credentials, query, or fragment")
    if interval < 1:
        raise ValueError("Interval must be at least one second")
    config = config.expanduser().absolute()
    value = {"url": url.rstrip("/"), "targets": targets(choice), "interval": interval,
             "token": os.environ.get("SKILLHUB_TOKEN")}
    with lock(config.parent / "sync.lock"):
        _, count = sync(with_projects(value, config))
        atomic_json(config, value)
    print(f"Synchronized {count} skill folders. Configuration: {config}", flush=True)
    if count:
        print(SKILLS_UPDATED_NOTICE, flush=True)
    if autostart:
        print(f"Login startup installed: {enable_startup(config)}", flush=True)
    if start:
        start_background(config)
        print(f"Background sync started; log: {config.parent / 'client.log'}", flush=True)
