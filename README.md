# SkillHub

A Python service that turns a directory of skills into a shared library for **Cursor, OpenCode, Codex, and Claude Code**. Drop a skill folder into the service directory; connected users receive it automatically. The repository contains the service, client, and setup scripts. No marketplace or package publishing is required.

## Start locally

Requires Python 3.11+ and Git. From your clone:

```sh
./scripts/start.sh
```

On Windows, from PowerShell:

```powershell
./scripts/start.ps1
```

The script creates `.venv`, installs the project, starts the service at `http://127.0.0.1:8765`, and on first launch asks whether to set up **Cursor, OpenCode, Codex, Claude Code, all four, or server only** (the existing Cursor + OpenCode option is also available). Selecting a harness downloads skills immediately and starts a background sync process. Open the harness and use the installed `example-review` skill.

Setup writes skills to the selected harness's global directory:

- Cursor: `~/.cursor/skills/<name>/`
- OpenCode: `$XDG_CONFIG_HOME/opencode/skills/<name>/`, defaulting to `~/.config/opencode/skills/<name>/`
- Codex: `~/.agents/skills/<name>/`
- Claude Code: `~/.claude/skills/<name>/`

Choose adapters individually, combine them by repeating `--harness`, or select all four:

```sh
./scripts/setup.sh --harness codex
./scripts/setup.sh --harness claude-code
./scripts/setup.sh --harness codex --harness claude-code
./scripts/setup.sh --harness all
```

The same flags work with `scripts/setup.ps1` and `skillhub setup`. `--harness both` continues to mean Cursor + OpenCode. Rerunning setup replaces the selected targets in the client configuration; copies in deselected harnesses remain installed but stop receiving updates. Codex uses its current documented user skill directory, `~/.agents/skills`. Claude Code setup uses the default `~/.claude` directory; for a custom Claude configuration directory, adjust `targets` in the client config before syncing.

The client polls every five seconds. It runs independently of the harness, so files are ready when the harness opens. Harness discovery and conversational context are controlled by the harness itself: **start a new session or reload the harness if a running session does not pick up a new skill**. This project does not promise hot injection into an existing conversation. Skills provide instructions; they do not install tools or runtime dependencies referenced by those instructions.

## Share one service with multiple users

On the service machine, bootstrap once and run the noninteractive server:

```sh
./scripts/bootstrap.sh
.venv/bin/skillhub serve --host 0.0.0.0 --port 8765 --skills ./skills
```

Each user clones this repository onto their own machine and runs:

```sh
./scripts/setup.sh --url http://YOUR-SERVER:8765
# Or select explicitly:
./scripts/setup.sh --url http://YOUR-SERVER:8765 --harness both
```

Windows equivalent:

```powershell
./scripts/setup.ps1 --url http://YOUR-SERVER:8765 --harness both
```

Keep the clone and its virtual environment in place: the background client uses them. Keep the server running in a terminal, or supervise `skillhub serve` with your existing service manager. The setup scripts install **client** login startup, not a persistent server service.

The default server binds only to loopback. For a shared deployment use a trusted network or an HTTPS reverse proxy. Optional bearer authentication uses the same environment variable on the server and during client setup:

```sh
export SKILLHUB_TOKEN='your-shared-token'
.venv/bin/skillhub serve --host 0.0.0.0
# On each client, set SKILLHUB_TOKEN before running setup.
```

Setup saves the token in the user's client config, with owner-only file permissions on Unix. HTTPS is needed to protect the token in transit across untrusted networks. This is a read-only distribution service for trusted skill authors, not a public upload service. Files and scripts in skills are delivered unchanged; SkillHub does not execute them.

## Add and update skills

```text
skills/
  example-review/
    SKILL.md
    references/
      checklist.md
  your-skill/
    SKILL.md
    scripts/
      helper.py
```

Every non-hidden subdirectory must contain a UTF-8 `SKILL.md` with YAML frontmatter:

```markdown
---
name: your-skill
description: Explain what this skill does and when the agent should use it.
---

Your instructions here. Reference supporting files using relative paths.
```

The name must match the folder, use lowercase letters/digits separated by single hyphens, and be at most 64 characters. Descriptions must be 1–1024 characters. Additional frontmatter is preserved. Supporting files and Unix executable bits are preserved; hidden files are excluded and symlinks are rejected. Catalogs are limited to 64 MiB and 4096 files.

Validate before moving a new skill into the shared directory:

```sh
.venv/bin/skillhub validate --skills ./skills
```

The service rescans on requests, so there is no restart or publish step. Stage unfinished skills outside `skills/` (or in a hidden folder), then move them into place. If any skill is invalid, delivery pauses with an error and clients keep their last successful copies. Removing a skill removes its managed client copies on the next successful sync.

The client refuses to overwrite existing unmanaged skills or locally edited managed skills, including extra files. Resolve a collision by moving the local folder out of the harness's skills directory or renaming the server skill (including its frontmatter). Edit shared skills on the server, not in downloaded copies. Only folders owned by the configured service are updated or removed.

## Client controls

After bootstrap, run `.venv/bin/skillhub` on Unix or `.venv/Scripts/skillhub.exe` on Windows:

```sh
skillhub status
skillhub sync                         # Fetch immediately
skillhub watch                        # Foreground client (also suitable for a service manager)
skillhub stop                         # Stop the current background client
skillhub stop --disable-autostart     # Also remove login startup
skillhub setup --harness both         # Configure/start again
```

Use the full virtual-environment path unless it is activated. Configuration defaults to `~/.config/skillhub/client.json`; logs are alongside it in `client.log`. `--config PATH` is available on client commands. One configured service per client config is supported. Use a stable service URL: ownership is tied to that URL.

Login startup uses a LaunchAgent on macOS, XDG desktop autostart on Linux, and the Startup folder on Windows. Linux desktop startup requires a graphical login; for a headless Linux client, supervise `skillhub watch` with your service manager instead. Setup starts the client immediately in all cases. `--no-autostart` skips login startup; `--no-start` skips the background process. `--interval SECONDS` controls polling. Startup registration is per user, with one standard startup entry; custom configs do not create multiple login jobs.

Stopping the server or losing connectivity leaves the last downloaded skills usable. The client retries and logs failures. Sync validates the complete archive before installation, preflights conflicts across all selected targets, stages each skill on its destination filesystem, and recovers interrupted folder replacements. Replacement is per skill; it is not a transaction spanning all skills/harnesses. There can be a brief directory-swap gap. Local concurrent edits during a sync are not supported.

## Opt in individual projects

Keep a separate, per-user list of projects that should receive the shared library:

```sh
.venv/bin/skillhub projects add ~/Code/my-project --harness codex --harness claude-code
.venv/bin/skillhub projects list
.venv/bin/skillhub sync
.venv/bin/skillhub projects remove ~/Code/my-project
```

On Windows, use `.venv/Scripts/skillhub.exe`. Omit `--harness` to select all four tools. Adding an existing project replaces its harness selection. Paths supplied to the command can be relative; the list stores absolute paths. Project directories must already exist. No projects are opted in automatically.

The list lives in `~/.config/skillhub/projects.json`, separately from `client.json`. With `--config PATH` (placed after `projects add`, `list`, or `remove`), it lives beside that client config. Configs in the same directory share the list and sync lock. You can also edit the list directly:

```json
[
  {
    "path": "/absolute/path/to/my-project",
    "harnesses": ["codex", "claude-code"]
  }
]
```

Each opted-in project receives skills under `.cursor/skills`, `.opencode/skills`, `.agents/skills` (Codex), or `.claude/skills`, according to its selection. These are additional destinations; global sync continues. All shared skills are included. Rerunning setup preserves the separate project list.

The background watcher reloads the list each poll and downloads for new destinations even when the server catalog is unchanged. `skillhub sync` applies it immediately. A watcher started before upgrading to this feature needs a one-time restart. Missing project directories are skipped without recreating them, and sync resumes when they return. Invalid lists pause sync with a logged error.

Removing a project or deselecting a harness stops its updates and leaves existing copies intact. While opted in, server updates/removals and local-edit protection work just like global sync; a conflicting project skill pauses the entire sync. Project files may appear in Git status; SkillHub does not edit your ignore rules. Local delivery does not invalidate a harness's own skill/menu cache, including T3 Code's cached project lists.

When setup or sync adds, updates, or removes skill folders, it prints “Skills updated—restart T3 Code if the changes don't appear.” The background watcher writes the same notice to `client.log`; unchanged polls stay quiet. If T3 Code still shows stale skills, restart T3 Code (including its backend). SkillHub does not restart it automatically or modify T3 Code.

## HTTP interfaces

- `GET /healthz`: process health (does not validate skills).
- `GET /v1/catalog`: validated manifest with file hashes and content versions.
- `GET /v1/bundle.zip`: one consistent snapshot, with ETag/conditional request support; used by the sync client.
- `GET /.well-known/skills/index.json`: OpenCode-compatible remote skill catalog.
- `GET /.well-known/skills/<name>/<path>`: native catalog files.

For OpenCode versions supporting remote skills, you can configure the native endpoint instead of using the sync client:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": ["http://YOUR-SERVER:8765/.well-known/skills/"]
}
```

Some older versions use `"skills": {"urls": ["..."]}`. Native refresh timing is controlled by OpenCode; the sync client is the recommended route for predictable polling. Native configuration does not include bearer-header setup, so use the client for token-protected services. Do not enable both delivery routes for the same OpenCode library. The native endpoint consists of separate file requests; concurrent server edits can span versions. The sync client's bundle avoids that issue.

Commands are intentionally separate: this version distributes skills and their supporting files, not OpenCode command definitions. Harness-specific fields, referenced tools, and tool permissions remain the author's responsibility. Setup targets local desktop/CLI harnesses; remote/cloud agents need their own integration or client.

## Development and verification

```sh
./scripts/bootstrap.sh
.venv/bin/python -m unittest discover -s tests -v
```

Integration tests run a real local HTTP server and use temporary client homes. They cover all four destinations, updates/removals, ETags, authentication, supporting files, conflicts, local edits, invalid catalogs, traversal, integrity, symlinks, offline retention, interrupted replacements, and setup/background sync/stop. Harness applications are not automated by this test suite. macOS is the development platform; Windows and Linux startup scripts still need testing on those operating systems.

The server uses Python's standard-library threaded HTTP server and PyYAML. It rescans/builds a snapshot on each request, including conditional requests: suitable for an initial small shared library, not yet optimized for large fleets. No hosted service, remote repository, or marketplace is created by setup.

References: [Cursor skills](https://cursor.com/docs/skills), [OpenCode skills](https://opencode.ai/docs/skills/), [OpenCode configuration schema](https://opencode.ai/config.json), [Codex skills](https://developers.openai.com/codex/skills/), [Claude Code skills](https://code.claude.com/docs/en/skills).
