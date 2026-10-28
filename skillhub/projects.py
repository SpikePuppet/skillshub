"""Explicit per-client project opt-ins, stored separately from server settings."""
import json
import logging
from pathlib import Path


PROJECT_PATHS = {
    "cursor": ".cursor/skills",
    "opencode": ".opencode/skills",
    "codex": ".agents/skills",
    "claude-code": ".claude/skills",
}
HARNESS_CHOICES = (*PROJECT_PATHS, "both", "all")


def harnesses(choices):
    choices = [choices] if isinstance(choices, str) else choices
    if not isinstance(choices, list) or not choices or any(c not in HARNESS_CHOICES for c in choices):
        raise ValueError("Choose a supported harness: " + ", ".join(HARNESS_CHOICES))
    selected = set(choices)
    if "both" in selected:
        selected.update(("cursor", "opencode"))
    if "all" in selected:
        selected.update(PROJECT_PATHS)
    return [name for name in PROJECT_PATHS if name in selected]


def projects_path(config):
    return config.parent / "projects.json"


def read_projects(config):
    path = projects_path(config)
    if not path.exists():
        return []
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"{path} must contain a JSON list")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            raise ValueError("Each project needs a path and harnesses")
        root = Path(entry["path"]).expanduser()
        if not root.is_absolute():
            raise ValueError(f"Project paths must be absolute: {root}")
        result.append({"path": str(root), "harnesses": harnesses(entry.get("harnesses"))})
    return result


def with_projects(value, config):
    targets = list(value["targets"])
    for entry in read_projects(config):
        root = Path(entry["path"])
        if not root.is_dir():
            logging.warning("Skipping unavailable project: %s", root)
            continue
        for name in entry["harnesses"]:
            relative = Path(PROJECT_PATHS[name])
            destination = root / relative
            # Do not follow a harness directory redirected outside the opt-in.
            if any((root / parent).is_symlink() for parent in (relative, *relative.parents)):
                raise ValueError(f"Project skill path must not contain a symlink: {destination}")
            targets.append(str(destination))
    return {**value, "targets": list(dict.fromkeys(targets))}


def read_config(config):
    return with_projects(json.loads(config.read_text()), config)


def settings_signature(config):
    """Changes whenever client.json or projects.json is rewritten."""
    stamps = []
    for path in (config, projects_path(config)):
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def update_project(config, project, choices=None):
    # Share the sync lock so a completed removal cannot race a stale sync.
    from .client import atomic_json, wait_lock
    root = project.expanduser().resolve()
    if choices is not None and not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    with wait_lock(config.parent / "sync.lock"):
        entries = [entry for entry in read_projects(config)
                   if Path(entry["path"]).resolve() != root]
        if choices is not None:
            entries.append({"path": str(root), "harnesses": harnesses(choices)})
        atomic_json(projects_path(config), entries)
