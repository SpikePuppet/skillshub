"""Validated, deterministic snapshots of a directory of skills."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

import yaml

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"Invalid relative path: {value}")
    return p


def metadata(data, directory):
    text = data.decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise ValueError(f"{directory}/SKILL.md requires YAML frontmatter")
    meta = yaml.safe_load(match[1])
    if not isinstance(meta, dict):
        raise ValueError(f"{directory}: invalid frontmatter")
    name = meta.get("name")
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name) or name != directory:
        raise ValueError(f"{directory}: name must match folder and use lowercase words separated by hyphens")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError(f"{directory}: description must be 1–1024 characters")
    return {"name": name, "description": description}


def snapshot(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"Skills directory does not exist: {root}")
    files, skills, total = {}, [], 0
    for folder in sorted(root.iterdir()):
        if folder.name.startswith("."):
            continue
        if folder.is_symlink():
            raise ValueError(f"Symlinks are not supported: {folder}")
        if not folder.is_dir():
            continue
        if not (folder / "SKILL.md").is_file():
            raise ValueError(f"{folder.name}: missing SKILL.md (stage unfinished skills outside the directory)")
        members = {}
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if any(p.startswith(".") for p in rel.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Symlinks are not supported: {path}")
            if not path.is_file():
                continue
            key = f"{folder.name}/{rel.as_posix()}"
            safe_path(key)
            if path.stat().st_size > MAX_BYTES:
                raise ValueError(f"File too large: {path}")
            data = path.read_bytes()
            total += len(data)
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                raise ValueError("Catalog exceeds 64 MiB or 4096 files")
            files[key] = data
            members[rel.as_posix()] = {"sha256": digest(data), "executable": bool(path.stat().st_mode & 0o111)}
        info = metadata(files[f"{folder.name}/SKILL.md"], folder.name)
        version = digest(json.dumps(members, sort_keys=True).encode())
        skills.append({**info, "version": version, "files": members})
    manifest = {"schema": 1, "skills": skills}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in {"manifest.json": json.dumps(manifest, sort_keys=True).encode(), **files}.items():
            archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data)
    return manifest, files, out.getvalue()
