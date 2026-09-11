# Codex Session Backup

Back up and restore Codex conversations by project. Export one project or all your projects, check your backups offline, and restore them on another machine or at a new project path.

An independent community tool, not affiliated with or endorsed by OpenAI. [MIT licensed](LICENSE).

## Get started

Requires **Python 3.10 or newer**. No extra Python packages, account login, or network access are needed to run the tool.

```bash
git clone https://github.com/MisterSheikh/codex-session-backup.git
cd codex-session-backup
python3 codex_sessions.py --help
```

Keep the repository's Python files together. The default session location is `~/.codex`; `CODEX_HOME` or `--codex-home /another/.codex` can override it.

**Close Codex before making your final export or restoring sessions.** Back up your project files separately. Backup folders must be new; existing sessions are never silently overwritten.

## Back up one project

```bash
python3 codex_sessions.py export ~/my-project ~/my-project-backup --include-attachments
```

This includes sessions started in the project and its subdirectories, including archived sessions. Omit `--include-attachments` if you only want conversation data; external attachment references will still be reported.

## Back up all projects

Preview how sessions will be grouped:

```bash
python3 codex_sessions.py export-all ~/codex-session-backups --dry-run
```

Create the backups:

```bash
python3 codex_sessions.py export-all ~/codex-session-backups --include-attachments
```

Each project gets an individually restorable subdirectory. Linked conversation segments are included automatically; keep the entire folder together. `index.json` lists the projects and any problems. Sessions with unclear project ownership, and extra versions of duplicate sessions, are preserved under `_unassigned` for manual review; that folder is not a normal restorable project backup.

For later snapshots, choose a new destination such as `~/codex-session-backups-next`.

## Verify a backup

After copying a backup to another disk or machine:

```bash
# One project
python3 codex_sessions.py verify ~/my-project-backup

# An entire collection
python3 codex_sessions.py verify ~/codex-session-backups
```

Verification runs offline and does not change your backup or Codex installation. It checks files, checksums, conversation IDs, saved metadata, history offsets, and bundled attachments.

Read the JSON result:

- **`valid: true`**: the implemented integrity checks passed.
- **`complete: false`**: read `warnings` and `errors`. Files may be unresolved, some sessions may need manual attention, or an older backup may lack attachment information.
- **`resume_tested: false`**: verification does not launch Codex. It cannot promise that a particular Codex version will resume the conversations.

Exit codes: **0** = checks passed without warnings; **1** = error or invalid backup; **2** = attention needed, such as unresolved attachments. An export returning 2 may still have created usable project backups—read its report.

## Restore one project

On the destination machine, install and launch Codex once, then close it. Copy the **entire project backup folder**, including any `assets` directory, and run:

```bash
python3 codex_sessions.py restore ~/my-project-backup ~/projects/my-project
```

The final argument is the new project location. Omit it to keep the original paths. To restore one project from a bulk collection, point at that project's subdirectory:

```bash
python3 codex_sessions.py restore ~/codex-session-backups/my-project ~/projects/my-project
cd ~/projects/my-project
codex resume
```

Archived and noninteractive sessions retain their normal Codex visibility rules. Refresh or restart Desktop if its list is stale. Keep your original environment or a private fallback until you have inspected restored conversations on the destination.

## What about images and attachments?

Images embedded directly in conversations are already backed up. With `--include-attachments`, the tool also bundles recognizable referenced files inside Codex's managed upload and generated-image directories. Their references are updated during restore.

It does **not** download remote URLs, copy arbitrary files from your project or elsewhere, or copy the global attachment registry. Missing files and recognized references that cannot be bundled are reported. Detection is conservative: a clean report is not proof that every possible external artifact was found. See the [attachment investigation](docs/attachments.md) for coverage and limitations.

## Compatibility and privacy

Tested with **Codex CLI 0.154.0**. Codex's internal storage can change; prefer matching versions for migration. Backups from the original version-1 format remain supported, but verification reports their unknown attachment completeness. New exports use format version 3, which also preserves linked history segments. Ordinary version-1 and version-2 backups remain supported; older backups missing linked segments must be re-exported from the original installation.

Credentials, global configuration, and caches are excluded. **Conversations and attachments can themselves contain secrets or personal information. Keep your backups private.** Checksums detect corruption; they do not establish who created or modified a backup.

For storage details, failure behavior, and testing instructions, see [implementation notes](docs/implementation.md).
