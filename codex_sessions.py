#!/usr/bin/env python3
"""Portable, project-scoped Codex session backups (Python standard library only)."""
import argparse
import hashlib
import json
import os
import ntpath
import posixpath
import re
from pathlib import Path, PureWindowsPath, PurePosixPath
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone

FORMAT = 1
HISTORY = ('thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items')
RELATED = ('thread_dynamic_tools',)
# Only session metadata, never installation/project/account records.
FIELDS = set('id rollout_path created_at updated_at source model_provider cwd title sandbox_policy approval_mode tokens_used has_user_event archived archived_at git_sha git_branch git_origin_url cli_version first_user_message agent_nickname agent_role memory_mode model reasoning_effort agent_path created_at_ms updated_at_ms thread_source preview recency_at recency_at_ms history_mode name originator daybreak_enabled'.split())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def relative(cwd, root):
    cls = PureWindowsPath if PureWindowsPath(root).drive else PurePosixPath
    try:
        normalize = ntpath.normpath if cls is PureWindowsPath else posixpath.normpath
        return cls(normalize(cwd)).relative_to(cls(normalize(root))).parts
    except (ValueError, TypeError):
        return None


def connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def database(home, pattern, table):
    found = []
    for p in home.glob(pattern):
        with connect(p) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                found.append(p)
    if len(found) != 1:
        raise ValueError(f'Expected one initialized {table} database in {home}; found {len(found)}. Start/close the target Codex first, or resolve obsolete databases.')
    return found[0]


def rows(db, table, sid, key='thread_id'):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
        return []
    return [dict(r) for r in db.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (sid,))]


def meta(data):
    first = json.loads(data.splitlines()[0])
    return first['payload'] if first.get('type') == 'session_meta' else first


def file_meta(path):
    with path.open('rb') as f:
        return meta(f.readline())


def rollout_files(home):
    for folder in ('sessions', 'archived_sessions'):
        yield from (home / folder).rglob('*.jsonl')


def export(home, project, destination, *, selected_paths=None):
    project = str(Path(project).expanduser().resolve()) if selected_paths is None else project
    if destination.exists():
        raise ValueError(f'Destination already exists: {destination}')
    state = database(home, 'state_*.sqlite', 'threads')
    history = database(home, 'thread_history_*.sqlite', 'thread_turns')
    sessions, payloads, seen, skipped = [], {}, set(), 0
    with connect(state) as db, connect(history) as hd:
        db.execute('BEGIN'); hd.execute('BEGIN')
        indexed = {r['id']: dict(r) for r in db.execute('SELECT * FROM threads')}
        paths = set(selected_paths) if selected_paths is not None else set(rollout_files(home))
        if selected_paths is None:
            paths.update(Path(r['rollout_path']) for r in indexed.values() if relative(r['cwd'], project) is not None)
        for p in sorted(paths):
            if p.suffix != '.jsonl' or not any(p.resolve().is_relative_to((home/folder).resolve()) for folder in ('sessions','archived_sessions')):
                raise ValueError(f'Rollout outside supported session directories: {p}')
            with p.open('rb') as f:
                first = f.readline()
            m = meta(first)
            sid = m.get('id')
            row = indexed.get(sid, {})
            cwd = m.get('cwd') or row.get('cwd')
            if not cwd:
                skipped += 1
                continue
            if relative(cwd, project) is None:
                continue
            if str(uuid.UUID(sid)) != sid or sid in seen:
                raise ValueError(f'Invalid or duplicate session ID: {sid}')
            seen.add(sid)
            before = p.stat()
            data = p.read_bytes()
            for line in data.splitlines():
                json.loads(line)
            after = p.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f'Session changed during export; stop it and retry: {sid}')
            item = {'id': sid, 'cwd': cwd, 'original_rollout_path': str(p), 'timestamp': m.get('timestamp'),
                    'archived': bool(row.get('archived', 'archived_sessions' in p.parts)),
                    'file': f'rollouts/{sid}.jsonl', 'sha256': hashlib.sha256(data).hexdigest(),
                    'thread': {k:v for k,v in row.items() if k in FIELDS},
                    'related': {t: rows(db,t,sid) for t in RELATED},
                    'history': {t: rows(hd,t,sid) for t in HISTORY}}
            if not row:
                created = int(datetime.fromisoformat(m['timestamp'].replace('Z','+00:00')).timestamp())
                item['thread'] = dict(id=sid, rollout_path=str(p), cwd=cwd, created_at=created,
                    updated_at=int(after.st_mtime), source=m.get('source','cli') if isinstance(m.get('source','cli'),str) else json.dumps(m['source']),
                    model_provider=m.get('model_provider','openai'), title='', sandbox_policy='"read-only"',
                    approval_mode='on-request', archived=int(item['archived']), history_mode=m.get('history_mode','legacy'))
                if item['thread']['history_mode'] != 'legacy':
                    raise ValueError(f'Paginated session {sid} lacks an index; repair it with Codex before export.')
            sessions.append(item); payloads[item['file']] = data
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        stage = Path(staging)/'backup'; (stage/'rollouts').mkdir(parents=True, mode=0o700)
        for name,data in payloads.items():
            (stage/name).write_bytes(data); (stage/name).chmod(0o600)
        manifest = {'format':'codex-project-sessions','version':FORMAT,'exported_at':datetime.now(timezone.utc).isoformat(),
                    'project_path':project,'source_databases':[state.name,history.name], 'sessions':sessions}
        (stage/'manifest.json').write_text(dumps(manifest)); (stage/'manifest.json').chmod(0o600)
        stage.rename(destination)
    return {'exported':len(sessions),'backup':str(destination),'skipped_without_cwd':skipped}


def project_root(cwd, registered=()):
    """Prefer registered roots, then nearest local Git root, then exact cwd."""
    candidates = [root for root in registered if relative(cwd, root) is not None]
    if candidates:
        return max(candidates, key=len), 'codex-project-root'
    # Do not interpret foreign Windows paths as local POSIX directories.
    if not PureWindowsPath(cwd).drive or os.name == 'nt':
        path = Path(cwd)
        if path.is_absolute():
            for parent in (path, *path.parents):
                if (parent/'.git').exists():
                    return str(parent), 'git-root'
    return cwd, 'recorded-cwd'


def export_all(home, destination, dry_run=False):
    if destination.exists():
        raise ValueError(f'Destination already exists: {destination}')
    state = database(home, 'state_*.sqlite', 'threads')
    with connect(state) as db:
        indexed = {r['id']: dict(r) for r in db.execute('SELECT * FROM threads')}
        registered = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='project_roots'").fetchone():
            registered = [r[0] for r in db.execute('SELECT path FROM project_roots')]
    paths = set(rollout_files(home))
    paths.update(Path(r['rollout_path']) for r in indexed.values())
    groups, unassigned = {}, []
    for path in sorted(paths):
        try:
            m = file_meta(path)
            sid = m.get('id')
            cwd = m.get('cwd') or indexed.get(sid, {}).get('cwd')
            canonical = Path(indexed[sid]['rollout_path']) if sid in indexed else None
            if canonical and canonical.resolve() != path.resolve() and canonical.is_file() and file_meta(canonical).get('id') == sid:
                unassigned.append({'rollout_path': str(path), 'id': sid, 'cwd': cwd,
                    'error': 'Additional rollout for the same ID; the indexed rollout is exported in its project backup',
                    'indexed_rollout_path': str(canonical)})
                continue
            if not cwd:
                raise ValueError('No recorded cwd; project cannot be determined reliably')
            root, reason = project_root(cwd, registered)
            group = groups.setdefault(root, {'paths': [], 'sessions': [], 'reasons': set()})
            group['paths'].append(path)
            group['sessions'].append({'id': sid, 'cwd': cwd})
            group['reasons'].add(reason)
        except (OSError, ValueError, KeyError, TypeError) as e:
            unassigned.append({'rollout_path': str(path), 'error': str(e)})
    report = {'format': 'codex-project-session-collection', 'version': 1,
              'exported_at': datetime.now(timezone.utc).isoformat(),
              'projects': [], 'unassigned': unassigned, 'exported': 0}
    used = {'_unassigned'}
    for root, group in sorted(groups.items()):
        basename = (PureWindowsPath(root) if PureWindowsPath(root).drive else PurePosixPath(root)).name
        name = re.sub(r'[^A-Za-z0-9_.-]+', '-', basename).strip('.-')[:80] or 'root'
        if name.casefold() in used:
            name += '-' + hashlib.sha256(root.encode()).hexdigest()[:12]
        used.add(name.casefold())
        report['projects'].append({'project_path': root, 'directory': name,
            'grouping': sorted(group['reasons']), 'sessions': group['sessions'], 'status': 'planned'})
    if dry_run:
        return report
    # Publish the collection only after every project has been attempted.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        stage = Path(staging)/'collection'; stage.mkdir(mode=0o700)
        for project in report['projects']:
            try:
                result = export(home, project['project_path'], stage/project['directory'],
                                selected_paths=groups[project['project_path']]['paths'])
                project['exported'] = result['exported']
                project['status'] = 'exported'
                report['exported'] += result['exported']
            except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as e:
                project['status'] = 'failed'
                project['error'] = str(e)
        # Keep unattributable legacy data without inventing restore metadata.
        report['preserved_unassigned'] = 0
        for number, entry in enumerate(unassigned):
            path = Path(entry['rollout_path'])
            try:
                if path.suffix != '.jsonl' or not any(path.resolve().is_relative_to((home/f).resolve()) for f in ('sessions','archived_sessions')):
                    raise ValueError('Outside supported rollout directories')
                before = path.stat(); data = path.read_bytes(); after = path.stat()
                if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                    raise ValueError('Rollout changed while copying')
                raw = stage/'_unassigned'/f'{number:04d}-{path.name}'
                raw.parent.mkdir(mode=0o700,exist_ok=True)
                raw.write_bytes(data); raw.chmod(0o600)
                entry['file'] = str(raw.relative_to(stage))
                entry['sha256'] = hashlib.sha256(data).hexdigest()
                report['preserved_unassigned'] += 1
            except (OSError,ValueError) as e:
                entry['copy_error'] = str(e)
        report['complete'] = not unassigned and all(p['status']=='exported' for p in report['projects'])
        (stage/'index.json').write_text(dumps(report)); (stage/'index.json').chmod(0o600)
        stage.rename(destination)
    return report


def remap(value, old, new):
    if isinstance(value, dict):
        out = {}
        for k,v in value.items():
            suffix = relative(v,old) if k in ('cwd','path') and isinstance(v,str) else None
            if k in ('workspace_roots','writable_roots') and isinstance(v,list):
                out[k] = [str(Path(new).joinpath(*relative(p,old))) if isinstance(p,str) and relative(p,old) is not None else remap(p,old,new) for p in v]
                continue
            out[k] = str(Path(new).joinpath(*suffix)) if suffix is not None else remap(v,old,new)
        return out
    if isinstance(value,list):
        return [remap(v,old,new) for v in value]
    return value


def rewrite(data, old, new):
    result, offsets, before, after = [], {}, 0, 0
    for line in data.splitlines(keepends=True):
        offsets[before] = after
        record = json.loads(line)
        if record.get('type') in ('session_meta','turn_context','world_state') or (record.get('type') == 'event_msg' and record.get('payload',{}).get('type') == 'thread_settings_applied'):
            record = remap(record,old,new)
        changed = (json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode()
        result.append(changed); before += len(line); after += len(changed)
    offsets[before] = after
    return b''.join(result), offsets


def insert(db, schema, table, row):
    columns = {r[1] for r in db.execute(f'PRAGMA {schema}.table_info("{table}")')}
    if not columns or set(row) - columns:
        raise ValueError(f'Incompatible target schema for {table}: {set(row)-columns}')
    names = ','.join('"'+k+'"' for k in row)
    db.execute(f'INSERT INTO {schema}."{table}" ({names}) VALUES ({",".join("?" for _ in row)})',list(row.values()))


def restore(home, backup, new_project=None):
    manifest = json.loads((backup/'manifest.json').read_text())
    if manifest.get('format') != 'codex-project-sessions' or manifest.get('version') != FORMAT:
        raise ValueError('Unsupported backup format/version')
    old = manifest['project_path']
    new = str(Path(new_project).expanduser().resolve()) if new_project else None
    state = database(home,'state_*.sqlite','threads')
    history = database(home,'thread_history_*.sqlite','thread_turns')
    prepared, ids = [], set()
    for s in manifest['sessions']:
        sid = s['id']
        if str(uuid.UUID(sid)) != sid or sid in ids:
            raise ValueError(f'Invalid/duplicate ID: {sid}')
        ids.add(sid)
        p = (backup/s['file']).resolve()
        if not p.is_relative_to(backup.resolve()):
            raise ValueError('Rollout path escapes backup')
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != s['sha256']:
            raise ValueError(f'Checksum mismatch: {sid}')
        if meta(data).get('id') != sid or s['thread'].get('id') != sid or set(s['thread'])-FIELDS:
            raise ValueError(f'Invalid metadata: {sid}')
        if relative(s['cwd'],old) is None:
            raise ValueError(f'Session outside backup project: {sid}')
        for line in data.splitlines(): json.loads(line)
        offsets = None
        if new: data,offsets = rewrite(data,old,new)
        # Codex scanning expects the conventional date hierarchy and UUID filename.
        stamp = s['timestamp'] or datetime.fromtimestamp(s['thread']['created_at'],timezone.utc).isoformat()
        dt = datetime.fromisoformat(stamp.replace('Z','+00:00'))
        folder = home/'archived_sessions' if s['archived'] else home/'sessions'/dt.strftime('%Y/%m/%d')
        target = folder/f'rollout-{dt.strftime("%Y-%m-%dT%H-%M-%S")}-{sid}.jsonl'
        row = dict(s['thread']); row['rollout_path'] = str(target)
        row['cwd'] = str(Path(new).joinpath(*relative(s['cwd'],old))) if new else s['cwd']
        if new and row.get('sandbox_policy'):
            row['sandbox_policy'] = json.dumps(remap(json.loads(row['sandbox_policy']),old,new))
        prepared.append((s,data,offsets,target,row))
    for p in rollout_files(home):
        if any(sid in p.name for sid in ids) or file_meta(p).get('id') in ids:
            raise ValueError(f'Existing rollout conflict: {p}')
    index = home/'session_index.jsonl'
    if index.exists():
        for line in index.read_text().splitlines():
            if json.loads(line).get('id') in ids:
                raise ValueError('Existing session_index entry conflicts with backup')
    created = []
    db = sqlite3.connect(state)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('ATTACH DATABASE ? AS hist',(str(history),))
        db.execute('BEGIN IMMEDIATE')
        for s,data,offsets,target,row in prepared:
            sid=s['id']
            for schema,tables in [('main',('threads',)+RELATED),('hist',HISTORY)]:
                for table in tables:
                    key = 'id' if table=='threads' else 'thread_id'
                    if db.execute(f'SELECT 1 FROM {schema}.{table} WHERE {key}=?',(sid,)).fetchone():
                        raise ValueError(f'Existing {table} conflict: {sid}')
            insert(db,'main','threads',row)
            for schema,group,allowed in [('main','related',RELATED),('hist','history',HISTORY)]:
                if set(s[group])-set(allowed): raise ValueError('Unknown session table')
                for table,entries in s[group].items():
                    for entry in entries:
                        entry=dict(entry)
                        if entry.get('thread_id') != sid: raise ValueError('Cross-session row in backup')
                        for k,v in list(entry.items()):
                            if offsets is not None and k.endswith('byte_offset') and v is not None:
                                if v not in offsets: raise ValueError(f'History offset is not a rollout boundary: {sid}')
                                entry[k]=offsets[v]
                        insert(db,schema,table,entry)
        for s,data,offsets,target,row in prepared:
            target.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            created.append(target)
            with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        db.commit()
    except BaseException:
        db.rollback()
        for p in created: p.unlink()
        raise
    finally:
        db.close()
    return {'restored':len(prepared),'codex_home':str(home)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME','~/.codex')).expanduser())
    sub=parser.add_subparsers(dest='command',required=True)
    exp=sub.add_parser('export');exp.add_argument('project_path');exp.add_argument('destination',type=Path)
    bulk=sub.add_parser('export-all', help='Export all sessions grouped into separate project backups')
    bulk.add_argument('destination',type=Path);bulk.add_argument('--dry-run',action='store_true')
    res=sub.add_parser('restore');res.add_argument('backup',type=Path);res.add_argument('new_project_path',nargs='?')
    args=parser.parse_args()
    try:
        home=args.codex_home.expanduser().resolve()
        if args.command == 'export-all':
            result=export_all(home,args.destination.expanduser().resolve(),args.dry_run)
        elif args.command == 'export':
            result=export(home,args.project_path,args.destination.expanduser().resolve())
        else:
            result=restore(home,args.backup.expanduser().resolve(),args.new_project_path)
        print(dumps(result),end='')
        if result.get('complete') is False:
            return 2
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as e:
        print(f'error: {e}',file=sys.stderr);return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
