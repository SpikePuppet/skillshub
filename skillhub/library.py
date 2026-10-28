"""Add skills to a served directory from dashboard uploads.

Uploads are a folder's files, a .zip, or a lone SKILL.md. Each skill is named by
its frontmatter, staged in a hidden folder inside the skills directory,
validated as a complete catalog, and only then moved into place.
"""
import base64
import binascii
import io
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

import yaml

from .catalog import MAX_BYTES, MAX_FILES, metadata, safe_path, snapshot

JUNK = {"__MACOSX", "Thumbs.db", "desktop.ini"}


class Conflict(ValueError):
    def __init__(self, names):
        super().__init__("Already in the library: " + ", ".join(names))
        self.names = names


def decode(entries):
    if not isinstance(entries, list) or not entries:
        raise ValueError("Nothing to add; drop a skill folder, a .zip, or a SKILL.md")
    files, total = {}, 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("data"), str):
            raise ValueError("Each file needs a path and base64 data")
        try:
            data = base64.b64decode(entry["data"], validate=True)
        except binascii.Error:
            raise ValueError(f"Could not decode {entry['path']}") from None
        total += len(data)
        if total > MAX_BYTES or len(files) >= MAX_FILES:
            raise ValueError("Upload exceeds 64 MiB or 4096 files")
        files[entry["path"].replace("\\", "/").strip("/")] = (data, None)
    if len(files) == 1 and next(iter(files)).lower().endswith(".zip"):
        return unzip(next(iter(files.values()))[0])
    return files


def unzip(data):
    files, total = {}, 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That .zip file could not be opened") from None
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError(f"Symlinks are not supported: {info.filename}")
            total += info.file_size
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                raise ValueError("Upload exceeds 64 MiB or 4096 files")
            files[info.filename.strip("/")] = (archive.read(info), bool(mode & 0o111) if mode else None)
    return files


def frontmatter_name(data, where):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", data.decode("utf-8", "replace"), re.S)
    meta = yaml.safe_load(match[1]) if match else None
    if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
        raise ValueError(f"{where} needs YAML frontmatter with a name")
    return meta["name"]


def group(files):
    """Split uploaded paths into {skill name: {relative path: (data, executable)}}."""
    kept = {}
    for path, value in files.items():
        safe_path(path)
        if any(p.startswith(".") or p in JUNK for p in path.split("/")):
            continue
        kept[path] = value
    if not kept:
        raise ValueError("Nothing to add; drop a skill folder, a .zip, or a SKILL.md")
    if "SKILL.md" in kept:
        folders = {"": kept}
    else:
        folders = {}
        for path, value in kept.items():
            top, _, rest = path.partition("/")
            if not rest:
                raise ValueError(f"{path} is outside a skill folder; every skill needs its own SKILL.md")
            folders.setdefault(top, {})[rest] = value
        # A zip of one wrapper folder holding several skill folders.
        if len(folders) == 1 and "SKILL.md" not in next(iter(folders.values())):
            return group(next(iter(folders.values())))
    skills = {}
    for folder, members in folders.items():
        where = f"{folder}/SKILL.md" if folder else "SKILL.md"
        if "SKILL.md" not in members:
            raise ValueError(f"{folder or 'Upload'}: missing SKILL.md")
        name = frontmatter_name(members["SKILL.md"][0], where)
        metadata(members["SKILL.md"][0], name)
        if name in skills:
            raise ValueError(f"Two uploaded skills are both named {name}")
        skills[name] = members
    return skills


def add_skills(root, entries, replace=False):
    """Validate and move uploaded skills into root. Returns the added names."""
    root = Path(root)
    skills = group(decode(entries))
    existing = sorted(name for name in skills if (root / name).exists() or (root / name).is_symlink())
    if existing and not replace:
        raise Conflict(existing)
    for name in existing:
        if (root / name).is_symlink() or not (root / name).is_dir():
            raise ValueError(f"{root / name} is not a skill folder; move it before replacing")
    stage = Path(tempfile.mkdtemp(prefix=".skillhub-upload-", dir=root))
    try:
        for name, members in skills.items():
            for relative, (data, executable) in members.items():
                path = stage / name / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                # Browsers drop file modes; treat scripts with a shebang as executable.
                if executable is None:
                    executable = data.startswith(b"#!")
                path.chmod(0o755 if executable else 0o644)
        snapshot(stage)
        for name in skills:
            destination = root / name
            if destination.exists():
                destination.rename(stage / (".previous-" + name))
            (stage / name).rename(destination)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return sorted(skills)
