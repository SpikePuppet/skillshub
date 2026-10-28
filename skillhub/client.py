"""Download complete snapshots, verify them, and update only owned skill folders."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

from .catalog import MAX_BYTES, MAX_FILES, NAME, digest, metadata, safe_path

MARKER = ".skillhub.json"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".skillhub-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another SkillHub process is already using this configuration") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [i.filename for i in entries]
        if len(names) != len(set(names)) or len(entries) > MAX_FILES + 1:
            raise ValueError("Duplicate entries or too many files")
        if sum(i.file_size for i in entries) > MAX_BYTES + 2 * 1024 * 1024:
            raise ValueError("Expanded catalog exceeds limit")
        for name in names:
            safe_path(name)
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != 1 or not isinstance(manifest.get("skills"), list):
            raise ValueError("Unsupported manifest")
        result, expected = {}, {"manifest.json"}
        for skill in manifest["skills"]:
            name = skill["name"]
            if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name) or name in result:
                raise ValueError("Invalid or duplicate skill name")
            members = {}
            for relative, info in skill["files"].items():
                safe_path(relative)
                if any(p.startswith(".") for p in relative.split("/")):
                    raise ValueError("Hidden files are not permitted in a skill bundle")
                key = f"{name}/{relative}"
                contents = archive.read(key)
                if digest(contents) != info["sha256"] or not isinstance(info["executable"], bool):
                    raise ValueError(f"Invalid file metadata: {key}")
                expected.add(key)
                members[relative] = contents
            metadata(members["SKILL.md"], name)
            result[name] = (skill["files"], members)
        if expected != set(names):
            raise ValueError("Bundle contains undeclared files")
        return result


def inventory(folder):
    found = {}
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Refusing to replace a folder containing a symlink: {folder}")
        if path.is_file() and path.name != MARKER:
            found[path.relative_to(folder).as_posix()] = {
                "sha256": digest(path.read_bytes()),
                "executable": bool(path.stat().st_mode & 0o111) if os.name != "nt" else False,
            }
    return found


def owned(folder, url):
    if folder.is_symlink() or not folder.is_dir() or not (folder / MARKER).is_file():
        raise ValueError(f"Local skill already exists; move it or rename the server skill: {folder}")
    record = json.loads((folder / MARKER).read_text())
    if record.get("url") != url:
        raise ValueError(f"Skill belongs to another service: {folder}")
    if inventory(folder) != record["files"]:
        raise ValueError(f"Local edits detected; move the edited folder before syncing: {folder}")
    return record


def install(skills, targets, url):
    # Preflight all targets before changing anything. Each skill is staged on its
    # destination filesystem; interrupted directory swaps recover on the next run.
    plans = []
    for target in targets:
        target = Path(target)
        if target.is_symlink():
            raise ValueError(f"Target must not be a symlink: {target}")
        target.mkdir(parents=True, exist_ok=True)
        for backup in target.glob(".skillhub-backup-*"):
            original = target / backup.name.removeprefix(".skillhub-backup-")
            owned(backup, url)
            if original.exists():
                owned(original, url)
                shutil.rmtree(backup)
            else:
                backup.rename(original)
        managed = {}
        for folder in target.iterdir():
            if folder.name.startswith(".") or not folder.is_dir():
                continue
            marker = folder / MARKER
            if marker.is_file() and json.loads(marker.read_text()).get("url") == url:
                managed[folder.name] = owned(folder, url)
        for name in skills:
            destination = target / name
            if destination.exists() or destination.is_symlink():
                managed[name] = owned(destination, url)
        for name, (info, members) in skills.items():
            local_info = {k: {**v, "executable": v["executable"] if os.name != "nt" else False} for k, v in info.items()}
            if name not in managed or managed[name]["files"] != local_info:
                plans.append((target, name, local_info, members))
        for name in managed.keys() - skills.keys():
            plans.append((target, name, None, None))
    for target, name, info, members in plans:
        destination = target / name
        backup = target / (".skillhub-backup-" + name)
        stage = Path(tempfile.mkdtemp(prefix=".skillhub-stage-", dir=target))
        try:
            if members is not None:
                for relative, data in members.items():
                    path = stage / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    path.chmod(0o755 if info[relative]["executable"] else 0o644)
                atomic_json(stage / MARKER, {"url": url, "files": info})
            if destination.exists():
                destination.rename(backup)
            try:
                if members is not None:
                    stage.rename(destination)
            except BaseException:
                if backup.exists():
                    backup.rename(destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return len(plans)


def sync(config, etag=None):
    headers = {"User-Agent": "SkillHub/0.1"}
    if config.get("token"):
        headers["Authorization"] = "Bearer " + config["token"]
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(config["url"].rstrip("/") + "/v1/bundle.zip", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(MAX_BYTES + 2 * 1024 * 1024 + 1)
            if len(data) > MAX_BYTES + 2 * 1024 * 1024:
                raise ValueError("Bundle exceeds download limit")
            new_etag = response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 304:
            return etag, 0
        raise
    skills = unpack(data)
    changed = install(skills, config["targets"], config["url"])
    return new_etag, changed
