# Compact project backups

`export --compact` and `export-all --compact` wrap ordinary version-3 project backups in a lossless container. This is independent of Codex's native storage format. `verify` and `restore` reconstruct ordinary files before running the existing validation/restoration logic. Bulk collection indexes remain ordinary JSON, and unassigned raw rollouts retain their existing preservation treatment.

## Layout

```
project/
  compact.json
  payload.tar.gz
```

`compact.json` identifies `codex-compact-project`, version 1, gzip compression, the archive's SHA-256, original file paths/sizes/SHA-256s, byte reconstruction recipes, and a blob index. The gzip archive contains regular files only:

- `files/000000`, etc.: original file bytes with extracted image spans removed.
- `blobs/<sha256>`: each unique decoded image payload once per project.

Each reconstruction recipe interleaves literal byte lengths from a body file with an image digest and its original data-URL prefix. This avoids placeholder collisions and preserves whitespace, escaping, line endings, JSON formatting, and base64 representations byte-for-byte. Media is scanned in `.json` and `.jsonl`, including serialized history JSON strings in manifests and linked segments. Other files are retained as ordinary bodies.

Only canonical, nonempty base64 image spans matching supported data-URL syntax are extracted. Unsupported/noncanonical representations remain untouched. Different prefixes can refer to the same binary blob. Base64 reconstruction uses chunk sizes divisible by three, followed by validation of every complete reconstructed file's original size and hash. No model or network call is involved.

This format changes neither original conversation data nor image quality. It does not make unsupported external attachments available: use `--include-attachments` for the existing managed-file collection policy.

## Validation and failure behavior

The reader validates the payload hash, object inventory, file recipes, sizes, object hashes, and reconstructed hashes. It accepts only the expected regular archive members with exact declared sizes, rejecting links, unexpected paths, duplicate members, and missing objects. It does not call `extractall`. Reconstructed paths must remain inside temporary storage and belong to the ordinary project layout.

The existing project verifier then validates conversation IDs, chains, row ownership, offsets, and attachment completeness. Restore only proceeds after reconstruction succeeds. Checksums establish internal consistency, not authenticity against an adversary who can replace both data and metadata.

Packing writes into staging and refuses an existing destination. Unpacking uses a private temporary directory that is removed on context exit, including ordinary failures. A process or machine crash can leave temporary data behind. There is no hard archive-size quota; temporary disk capacity must accommodate expanded bodies, image objects, and reconstructed files. A supplied container can request substantial resources, so process backups from sources you trust. Project packing stages the ordinary export too; it currently prioritizes correctness over minimum working space.

## Tests

```
python3 -m unittest discover -s tests -v
python3 tests/integration_codex.py ~/.codex --compact
python3 tests/integration_codex.py ~/.codex SESSION_ID --compact
```

Tests cover exact byte reconstruction, deduplication across nested JSON strings, retained unsupported encodings, ordinary and bulk verification, restoration, conflicting sessions, corrupt archives and blobs, bad recipes, missing objects, traversal, and archive links/duplicates. The installed-Codex test also covers native linked history and an inherited synthetic attachment.
