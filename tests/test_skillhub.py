import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile

from skillhub.catalog import snapshot
from skillhub.client import install, lock, sync, unpack
from skillhub.server import Server


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.write_skill("review", "First version")
        self.server = Server(("127.0.0.1", 0), self.skills, "test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.targets = [self.root / "cursor", self.root / "opencode"]
        self.config = {"url": self.url, "token": "test-secret", "targets": list(map(str, self.targets))}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def write_skill(self, name, text):
        folder = self.skills / name
        folder.mkdir(exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Review code.\n---\n{text}\n")
        (folder / "references").mkdir(exist_ok=True)
        (folder / "references/check.md").write_text("A supporting file")
        script = folder / "run.sh"
        script.write_text("#!/bin/sh\necho example\n")
        script.chmod(0o755)

    def get(self, route, token="test-secret", **headers):
        return urllib.request.urlopen(urllib.request.Request(self.url + route, headers={"Authorization": "Bearer " + token, **headers}))

    def test_delivery_updates_removal_and_cache(self):
        etag, count = sync(self.config)
        self.assertEqual(count, 2)
        for target in self.targets:
            self.assertEqual((target / "review/references/check.md").read_text(), "A supporting file")
            if os.name != "nt":
                self.assertTrue((target / "review/run.sh").stat().st_mode & 0o111)
        self.assertEqual(sync(self.config, etag), (etag, 0))
        self.write_skill("review", "Updated version")
        self.write_skill("new-skill", "Another skill")
        etag2, count = sync(self.config, etag)
        self.assertNotEqual(etag, etag2)
        self.assertEqual(count, 4)
        import shutil
        shutil.rmtree(self.skills / "review")
        self.assertEqual(sync(self.config)[1], 2)
        for target in self.targets:
            self.assertFalse((target / "review").exists())
            self.assertTrue((target / "new-skill/SKILL.md").exists())

    def test_local_conflict_preflights_all_targets(self):
        conflict = self.targets[1] / "review"
        conflict.mkdir(parents=True)
        (conflict / "SKILL.md").write_text("Personal skill")
        with self.assertRaisesRegex(ValueError, "already exists"):
            sync(self.config)
        self.assertFalse((self.targets[0] / "review").exists())
        self.assertEqual((conflict / "SKILL.md").read_text(), "Personal skill")

    def test_local_edits_are_retained(self):
        sync(self.config)
        local = self.targets[0] / "review/SKILL.md"
        local.write_text("My edits")
        self.write_skill("review", "Server edits")
        with self.assertRaisesRegex(ValueError, "Local edits"):
            sync(self.config)
        self.assertEqual(local.read_text(), "My edits")

    def test_invalid_catalog_retains_installed_skills(self):
        sync(self.config)
        (self.skills / "review/SKILL.md").write_text("Incomplete save")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            sync(self.config)
        self.assertEqual(caught.exception.code, 422)
        self.assertIn("First version", (self.targets[0] / "review/SKILL.md").read_text())

    def test_authentication_and_native_catalog(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/v1/catalog", token="wrong")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        with self.get("/.well-known/skills/index.json") as response:
            skill = json.load(response)["skills"][0]
            self.assertIn("references/check.md", skill["files"])
            self.assertTrue(skill["version"])
        with self.get("/.well-known/skills/review/SKILL.md") as response:
            self.assertIn(b"First version", response.read())
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/.well-known/skills/review/../../pyproject.toml")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    @unittest.skipIf(os.name == "nt", "Symlink permissions vary on Windows")
    def test_symlink_rejected(self):
        (self.skills / "review/leak").symlink_to(self.root / "private")
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            snapshot(self.skills)

    def test_interrupted_swap_recovers(self):
        sync(self.config)
        target = self.targets[0]
        (target / "review").rename(target / ".skillhub-backup-review")
        sync(self.config)
        self.assertTrue((target / "review/SKILL.md").exists())
        self.assertFalse((target / ".skillhub-backup-review").exists())

    def test_bundle_traversal_and_integrity(self):
        _, _, data = snapshot(self.skills)
        original = zipfile.ZipFile(io.BytesIO(data))
        for filename, payload in [("../escape", b"bad"), ("review/SKILL.md", b"corrupt")]:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                for name in original.namelist():
                    archive.writestr(name, payload if name == filename else original.read(name))
                if filename not in original.namelist():
                    archive.writestr(filename, payload)
            with self.assertRaises(ValueError):
                unpack(output.getvalue())

    def test_offline_retains_skills(self):
        sync(self.config)
        # A closed ephemeral port gives a deterministic connection failure.
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(urllib.error.URLError):
            sync({**self.config, "url": f"http://127.0.0.1:{port}"})
        self.assertTrue((self.targets[0] / "review/SKILL.md").exists())

    def test_setup_background_watch_and_stop(self):
        import subprocess
        import sys
        import time
        home = self.root / "home"
        home.mkdir()
        config = home / "client/client.json"
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "XDG_CONFIG_HOME": str(home / ".config"), "SKILLHUB_TOKEN": "test-secret"}
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "skillhub", *args], env=env,
                                  capture_output=True, text=True, timeout=30, check=True)
        try:
            cli("setup", "--url", self.url, "--harness", "both", "--interval", "1",
                "--no-autostart", "--config", str(config))
            cursor = home / ".cursor/skills/review/SKILL.md"
            opencode = home / ".config/opencode/skills/review/SKILL.md"
            self.assertTrue(cursor.exists())
            self.assertTrue(opencode.exists())
            self.write_skill("review", "Background update")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if cursor.exists() and "Background update" in cursor.read_text() and "Background update" in opencode.read_text():
                    break
                time.sleep(0.1)
            else:
                self.fail("Background update did not arrive")
            status = json.loads(cli("status", "--config", str(config)).stdout)
            self.assertTrue(status["running"])
        finally:
            cli("stop", "--config", str(config))
        self.assertFalse(json.loads(cli("status", "--config", str(config)).stdout)["running"])

    def test_snapshot_deterministic_and_lock_exclusive(self):
        self.assertEqual(snapshot(self.skills)[2], snapshot(self.skills)[2])
        with lock(self.root / "test.lock"):
            with self.assertRaises(RuntimeError):
                with lock(self.root / "test.lock"):
                    pass


if __name__ == "__main__":
    unittest.main()
