# Attachment investigation and supported coverage

Read-only inspection of the installed Codex CLI 0.154.0 state and its generated app-server JSON schemas was performed on September 10, 2026. No original sessions were restored or modified during investigation.

## Findings

The inspected installation had eight files under `CODEX_HOME/attachments`: seven upload/pasted-document files and one global `pasted-text-attachments.json` registry. Upload directories use UUIDs that are not necessarily thread IDs. The registry contains attachment paths, pending-removal paths, and text excerpts; it has no reliable per-thread ownership mapping and is not exported.

There were also 18 PNG files under `CODEX_HOME/generated_images/<thread-id>/`. Session ownership can be apparent from this directory, but the implementation only bundles files actually referenced in the selected session data; it does not copy entire directories.

A scan of rollout records found 7,086 `input_image` occurrences and 182 `local_image` occurrences. These are record occurrences, including repeated history, **not counts of unique attachments**. Sample image records contained inline `data:image/...;base64,...` payloads. Inline media already travels with the rollout. Local-image examples included relative paths to project files, which are not automatically copied.

Managed upload/image paths appeared in message text, completed items, tool output, and replacement history. Thus reading only first-line session metadata would miss them. The generated app-server protocol also describes local image/audio inputs, URL inputs, and generated-image `savedPath` values. Protocol support does not establish that each kind appeared in this installation.

## Implemented policy

Exports inventory known structured media fields throughout rollout JSON and projected `item_json`, plus recognizable references to the source Codex home's `attachments` and `generated_images` directories in string values. Inline data URLs are already retained. No remote URL is fetched.

With `--include-attachments`, referenced, existing regular files inside those managed directories are stored under `assets/<session-id>/<sha256>.<extension>`. The global pasted-text registry and symlinks escaping managed storage are excluded. Other local files, relative paths, and remote URLs remain unresolved, even if they appear to be useful media. This is intentional: blindly following paths in conversations could collect unrelated or sensitive files.

Restoration writes bundled files into `CODEX_HOME/attachments/restored/<session-id>/`, rejecting existing-file conflicts. Exact recorded reference strings are remapped in rollouts and projected history. Known attachment references in text are also remapped; ordinary project-path mentions remain unchanged. Rewriting rollouts updates their stored byte offsets. The original backup files remain unchanged.

## Limits

- Text-path detection assumes the recorded source-home prefix and conventional delimiters. Paths with spaces, escaping, Markdown encoding, or unfamiliar representations may not be recognized completely. Structured local-media fields preserve full path strings.
- Model/tool transcripts may mention a file without actually attaching it. A detected managed-file reference is evidence of relevance, not a guarantee of upload ownership.
- Embedded media is preserved byte-for-byte in the original rollout; verification validates JSON and whole-file checksums, not every media codec or base64 payload.
- Audio/file fields are recognized conservatively; they have not all been exercised through the installed server.
- Browser/Desktop rendering and arbitrary future Codex media formats are not certified. Tests cover packaging, reference/offset rewriting, history metadata, and restore behavior in disposable state.
- Existing backups are not modified automatically. Re-export with `--include-attachments` while the source files still exist to include them.

`verify` distinguishes integrity from detected completeness and always reports `resume_tested: false`. Version-1 archives retain their value but have no original attachment inventory, so their attachment completeness remains unknown.
