# Codex project session backups

An independent community tool, not affiliated with or endorsed by OpenAI.

A small Python 3.10+ CLI with no dependencies. Inspected and integration-tested against **codex-cli 0.154.0** and its installed app-server protocol on September 10, 2026.

Licensed under the [MIT License](LICENSE).

```bash
./codex_sessions.py export /path/to/project /path/to/new-backup
./codex_sessions.py restore /path/to/new-backup /path/to/moved-project
# Omit the last argument to keep the recorded project paths.
./codex_sessions.py --codex-home /path/to/.codex restore /path/to/new-backup /new/project
```

`CODEX_HOME` is honored; the default is `~/.codex`. Export destinations must not exist. Stop sessions being exported; close Codex CLI/Desktop on the restore target. For a fresh installation, launch Codex once and close it to initialize its state and history databases. The utility does not need authentication or network access. Use the same Codex version on both machines for the most predictable result.

Copy the entire backup directory to the other machine, including Windows/WSL. Recorded Windows paths can be mapped to a POSIX destination and vice versa; restore uses the destination Python platform's path conventions. Descendant working directories retain their relative suffix. Project files themselves are not backed up.

## Format and selection

Version 1 is `manifest.json` plus `rollouts/<session-id>.jsonl`. The manifest contains the original project path, export time, source database filenames, session IDs, original working directories and rollout paths, timestamps, titles/names and other selected thread metadata, SHA-256 checksums, and per-session history/tool rows as ordinary JSON. Exported rollout bytes are unchanged.

Selection uses the rollout's recorded `cwd` (falling back to its index for older formats), including the project root and descendants with path-component boundaries. Active and archived directories are scanned, including unindexed legacy rollouts. Ancient records with no attributable cwd are counted as skipped. Unindexed paginated sessions fail explicitly because their completeness cannot be established. The entire selected conversation is included even if individual turns visited another directory; fork/parent identifiers remain intact, but other projects' parent/child sessions are not automatically included.

Only session rollouts and allowlisted session rows are exported. No auth files, tokens from configuration, caches, global history, projects, memories, or whole SQLite databases are copied. **Conversation contents can themselves contain secrets or personal data**, as can titles and tool descriptions; protect the backup like the original conversations. External attachments/images and paths referenced in message text are not bundled.

## Restore behavior and observed indexing

Local inspection found `state_5.sqlite`'s `threads` table and paginated history in `thread_history_1.sqlite`. In an isolated experiment, copying a rollout alone allowed a metadata read by ID, but did not provide normal listing or paginated turns. Restoring the selected `threads`, `thread_dynamic_tools`, `thread_turns`, `thread_items`, `thread_realtime_items`, and `thread_history_projection_state` rows passed normal app-server list/read/turns/resume checks. No `session_index.jsonl` writes were needed; names are retained in the thread metadata. Existing entries in that file still count as conflicts.

The tool discovers databases by table presence and checks inserted columns against the target schema. It does not create/migrate Codex schemas or copy migration records. Multiple candidate databases, incompatible columns, or required target fields without defaults fail explicitly. This is deliberately conservative across future releases.

Restore preserves IDs, sources, archive status, provider/model metadata, and session-specific policies. It does not import global project/sidebar group associations. Archived and noninteractive sessions remain subject to Codex's normal filters (`codex resume --all`, `--include-non-interactive`, and Desktop archive views). Switch Desktop to the restored project and refresh/restart it if needed. Backend compatibility was tested; the Desktop GUI and an interactive TUI picker were not directly exercised.

Moving a project remaps structured `cwd`, workspace roots, and project-local permission paths in session metadata, turn context, world state and applied thread-settings events, plus the indexed cwd/sandbox policy. Literal conversation text and tool arguments are preserved. History byte offsets are recalculated to match rewritten JSONL lengths. References outside the original project remain unchanged.

All backup content is validated before session files are written. Conflicting IDs in rollouts, state/history rows, or the name index cause an error; there is no overwrite/merge option. SQLite inserts use a transaction, files use exclusive creation, and ordinary failures roll back inserted rows and remove newly created files. An abrupt process/machine crash can leave partial files or cross-database state (especially with SQLite WAL); a retry reports conflicts instead of overwriting them. Keep the original backup until you verify restoration.

## Tests

```bash
python3 -m unittest discover -s tests -v
# Optional installed-Codex integration test: reads and copies one CLI session,
# creates disposable databases, then lists/reads/resumes ONLY the copy.
python3 tests/integration_codex.py ~/.codex
```

The integration fixture copies schema/migration definitions to initialize its disposable databases; that is test scaffolding and is not part of export or restore. It does not send a model turn or copy authentication. Unit tests cover project boundaries, moved paths, history offsets, byte-preserving restoration, tampering, path traversal, conflicts, and rollback.

## Export every project at once

```bash
./codex_sessions.py export-all ~/codex-session-backups --dry-run
./codex_sessions.py export-all ~/codex-session-backups
# Restore just one of the resulting projects:
./codex_sessions.py restore ~/codex-session-backups/widaR ~/widaR
```

`export-all` scans session rollouts and index entries, assigns each rollout to one group, and writes a separate version-1 backup for each group. Existing selective `export` and `restore` commands still work. The destination must not already exist; use a new dated destination for subsequent snapshots.

Grouping prefers the most specific registered Codex project root, then the nearest existing Git root (including worktree `.git` files), then the exact recorded cwd. These are path-based heuristics: deleted/moved repositories fall back to recorded cwd, and separate checkouts stay separate. No conversation-text guesses are made. Home-directory sessions do not absorb nested project sessions. The top-level `index.json` records original roots, grouping reasons, session assignments and outcomes. Folder names use project basenames; collisions receive a path-derived suffix.

`--dry-run` prints the assignment plan without writing a backup. A real run attempts every project even if one fails. Unattributable rollouts are preserved as raw JSONL under `_unassigned`, with original paths and checksums in `index.json`; they are **not** normal restorable project backups until their project/metadata can be established. Missing/unreadable files and failed projects are reported explicitly. Exit code 2 means the collection needs attention (unassigned sessions or failed projects); exit code 1 means a top-level error. Successfully exported project directories remain individually restorable.

If multiple rollouts share a session ID, bulk export uses the file referenced by Codex's thread index for the restorable backup and preserves the other versions under `_unassigned`. This keeps the indexed history paired with its rollout without discarding older variants. The report explains these cases; `complete: false` can therefore mean all files were preserved but some require manual classification.
