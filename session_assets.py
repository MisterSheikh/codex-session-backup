"""Conservative media inventory: never fetch URLs or copy arbitrary local files."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


def asset_references(value, codex_home=None):
    """Return explicit media references and recognizable managed-file references."""
    refs = set()
    embedded = 0
    prefix = re.escape(str(codex_home)) if codex_home else ''
    managed = re.compile(prefix + r'[/\\](?:attachments|generated_images)[/\\][^\s<>"\x27`\\]+') if codex_home else None

    def add(v):
        nonlocal embedded
        if not isinstance(v, str) or not v:
            return
        if v.startswith('data:'):
            embedded += 1
        else:
            refs.add(v)

    def walk(x):
        if isinstance(x, dict):
            kind = x.get('type')
            if kind in ('localImage', 'local_image', 'localAudio', 'local_audio', 'imageView'):
                add(x.get('path'))
            if kind in ('image', 'input_image', 'audio', 'input_audio'):
                add(x.get('url') or x.get('image_url'))
            if kind in ('imageGeneration', 'image_generation_call'):
                add(x.get('savedPath') or x.get('saved_path'))
            if kind in ('input_file', 'file'):
                add(x.get('file_url') or x.get('file_path'))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and not x.startswith('data:'):
            if managed and ('attachments' in x or 'generated_images' in x):
                refs.update(m.group().rstrip(').,;]') for m in managed.finditer(x))
    walk(value)
    return refs, embedded


def inventory(records, history, home):
    refs, embedded = set(), 0
    for record in records:
        found, count = asset_references(record, home)
        refs.update(found); embedded += count
    for entries in history.values():
        for entry in entries:
            if entry.get('item_json'):
                found, _ = asset_references(json.loads(entry['item_json']), home)
                refs.update(found)
    return refs, embedded


def collect(refs, home, stage, sid, include):
    entries = []
    for ref in sorted(refs):
        entry = {'reference': ref, 'status': 'unresolved'}
        parsed = urlparse(ref)
        if parsed.scheme in ('http', 'https'):
            entry['reason'] = 'Remote media is not downloaded'
        else:
            path = Path(unquote(parsed.path)) if parsed.scheme == 'file' and parsed.netloc in ('','localhost') else Path(ref)
            roots = (home/'attachments', home/'generated_images')
            # Lexical AND resolved containment exclude traversal and symlink escapes.
            allowed = path.is_absolute() and any(path.is_relative_to(root) and path.resolve().is_relative_to(root) and root.resolve().is_relative_to(home.resolve()) for root in roots)
            allowed = allowed and path.name != 'pasted-text-attachments.json'
            if not allowed:
                entry['reason'] = 'Outside supported Codex attachment storage'
            elif not path.is_file():
                entry['reason'] = 'Referenced file is missing'
            elif not include:
                entry['reason'] = 'Not bundled; export with --include-attachments'
            else:
                before = path.stat(); data = path.read_bytes(); after = path.stat()
                if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                    raise ValueError(f'Attachment changed during export: {path}')
                digest = hashlib.sha256(data).hexdigest()
                # Keep filenames portable, independent of original upload names.
                suffix = path.suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,10}',path.suffix) else '.bin'
                name = f'assets/{sid}/{digest}{suffix}'
                target = stage/name; target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                target.write_bytes(data); target.chmod(0o600)
                entry.update(status='bundled',file=name,sha256=digest,size=len(data))
        entries.append(entry)
    return entries


def replace_references(value, mapping):
    if isinstance(value, dict):
        return {k: replace_references(v,mapping) for k,v in value.items()}
    if isinstance(value, list):
        return [replace_references(v,mapping) for v in value]
    if isinstance(value, str):
        if value in mapping:
            return mapping[value]
        # Only recognized attachment paths, not arbitrary project paths, in text.
        for old,new in sorted(mapping.items(),key=lambda pair:len(pair[0]),reverse=True):
            value = re.sub(re.escape(old)+r'(?![\w./\\-])',lambda match:new,value)
    return value
