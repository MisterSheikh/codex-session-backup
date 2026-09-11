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

from session_assets import inventory, collect, replace_references
from backup_verify import verify, checked_file

import compact_backup
import session_chains as chains

FORMAT = 3
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
    return chains.metadata(data)


def file_meta(path):
    with path.open('rb') as f:
        return meta(f.readline())


def rollout_files(home):
    for folder in ('sessions', 'archived_sessions'):
        yield from (home / folder).rglob('*.jsonl')


def export(home, project, destination, *, selected_paths=None, include_attachments=False, compact=False):
    if compact:
        if destination.exists():raise ValueError(f'Destination already exists: {destination}')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            raw=Path(tmp)/'project'
            result=export(home,project,raw,selected_paths=selected_paths,include_attachments=include_attachments)
            result.update(compact_backup.pack(raw,destination));result['backup']=str(destination)
            return result
    project = str(Path(project).expanduser().resolve()) if selected_paths is None else project
    if destination.exists():
        raise ValueError(f'Destination already exists: {destination}')
    state = database(home, 'state_*.sqlite', 'threads')
    history = database(home, 'thread_history_*.sqlite', 'thread_turns')
    sessions, payloads, seen, skipped = [], {}, set(), 0
    with connect(state) as db, connect(history) as hd:
        db.execute('BEGIN'); hd.execute('BEGIN')
        indexed = {r['id']: dict(r) for r in db.execute('SELECT * FROM threads')}
        candidates = chains.catalog(list(rollout_files(home)))
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
            canonical = Path(row['rollout_path']) if row else p
            if canonical != p and canonical.exists() and file_meta(canonical).get('id') == sid:
                if p in chains.resolve(canonical,candidates): continue
                raise ValueError(f'Additional unlinked rollout for session {sid}; use export-all to preserve it separately')
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
            chain_paths = chains.resolve(p,candidates)
            if len(chain_paths)>1:
                segments=[]
                for source in chain_paths:
                    if source.suffix!='.jsonl' or not any(source.resolve().is_relative_to((home/f).resolve()) for f in ('sessions','archived_sessions')):
                        raise ValueError('History dependency outside supported rollout storage')
                    before_segment=source.stat(); segment_data=source.read_bytes();after_segment=source.stat()
                    if (before_segment.st_size,before_segment.st_mtime_ns)!=(after_segment.st_size,after_segment.st_mtime_ns):raise ValueError('History segment changed during export')
                    if source==p and segment_data!=data:raise ValueError('Active segment changed during export')
                    sm=meta(segment_data);key=chains.thread_id(source,sm)
                    if relative(sm.get('cwd',cwd),project) is None:raise ValueError('Cross-project history dependency requires a separate explicit export strategy')
                    name=item['file'] if source==p else f'segments/{sid}/{key}.jsonl'
                    payloads[name]=segment_data
                    segment={'thread_id':key,'session_id':sm['id'],'file':name,'sha256':hashlib.sha256(segment_data).hexdigest(),
                        'original_rollout_path':str(source),'archived':('archived_sessions' in source.parts),'history':{t:rows(hd,t,key) for t in HISTORY},'related':{t:rows(db,t,key) for t in RELATED}}
                    segments.append(segment)
                chains.validate([(sg,payloads[sg['file']]) for sg in segments],segments[-1]['thread_id'])
                item['segments']=segments;item['active_thread_id']=segments[-1]['thread_id']
                item['history']={t:[] for t in HISTORY};item['related']={t:[] for t in RELATED}
            sessions.append(item); payloads[item['file']] = data
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        stage = Path(staging)/'backup'; (stage/'rollouts').mkdir(parents=True, mode=0o700)
        for name,data in payloads.items():
            (stage/name).parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            (stage/name).write_bytes(data); (stage/name).chmod(0o600)
        unresolved = 0
        for session in sessions:
            records = (json.loads(line) for line in payloads[session['file']].splitlines())
            refs, embedded = inventory(records,session['history'],home)
            for segment in session.get('segments',[]):
                segment_refs, count = inventory((json.loads(line) for line in payloads[segment['file']].splitlines()),segment['history'],home)
                refs.update(segment_refs)
                if segment['file']!=session['file']:embedded+=count
            session['assets'] = collect(refs,home,stage,session['id'],include_attachments)
            session['embedded_media_records'] = embedded
            unresolved += sum(a['status']=='unresolved' for a in session['assets'])
        manifest = {'format':'codex-project-sessions','version':FORMAT,'exported_at':datetime.now(timezone.utc).isoformat(),
                    'project_path':project,'source_codex_home':str(home),'skipped_without_cwd':skipped,'source_databases':[state.name,history.name], 'sessions':sessions}
        (stage/'manifest.json').write_text(dumps(manifest)); (stage/'manifest.json').chmod(0o600)
        stage.rename(destination)
    return {'exported':len(sessions),'backup':str(destination),'skipped_without_cwd':skipped,'unresolved_assets':unresolved,'complete':not unresolved and not skipped}


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


def export_all(home, destination, dry_run=False, include_attachments=False, compact=False):
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
    candidates=chains.catalog([p for p in paths if p.is_file()])
    dependencies=set()
    for row in indexed.values():
        try: dependencies.update(chains.resolve(Path(row['rollout_path']),candidates)[:-1])
        except (OSError,ValueError,KeyError): pass  # The owning project reports the failure during export.
    groups, unassigned = {}, []
    for path in sorted(paths):
        try:
            m = file_meta(path)
            sid = m.get('id')
            cwd = m.get('cwd') or indexed.get(sid, {}).get('cwd')
            canonical = Path(indexed[sid]['rollout_path']) if sid in indexed else None
            if canonical and canonical.resolve() != path.resolve() and canonical.is_file() and file_meta(canonical).get('id') == sid:
                if path in dependencies: continue
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
                                selected_paths=groups[project['project_path']]['paths'],include_attachments=include_attachments,compact=compact)
                project['storage']=result.get('storage','directory')
                project['unresolved_assets'] = result['unresolved_assets']
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
        report['complete'] = not unassigned and all(p['status']=='exported' and not p.get('unresolved_assets') for p in report['projects'])
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


def rewrite(data, old, new, assets=None):
    result, offsets, before, after = [], {}, 0, 0
    for line in data.splitlines(keepends=True):
        offsets[before] = after
        record = json.loads(line)
        if new and (record.get('type') in ('session_meta','turn_context','world_state') or (record.get('type') == 'event_msg' and record.get('payload',{}).get('type') == 'thread_settings_applied')):
            record = remap(record,old,new)
        if assets: record = replace_references(record,assets)
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
    if (backup/'compact.json').exists():
        with compact_backup.opened(backup) as expanded:
            return restore(home,expanded,new_project)
    manifest = json.loads((backup/'manifest.json').read_text())
    if manifest.get('format') != 'codex-project-sessions' or manifest.get('version') not in (1,2,FORMAT):
        raise ValueError('Unsupported backup format/version')
    if manifest['version'] >= 2:
        report = verify(backup)
        if not report['valid']: raise ValueError('; '.join(report['errors']))
    if any(s.get('segments') for s in manifest['sessions']):
        return chains.restore_native(home,backup,manifest,new_project)
    old = manifest['project_path']
    new = str(Path(new_project).expanduser().resolve()) if new_project else None
    state = database(home,'state_*.sqlite','threads')
    history = database(home,'thread_history_*.sqlite','thread_turns')
    prepared, ids, asset_files, asset_mappings = [], set(), {}, {}
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
        if meta(data).get('history_base') and not s.get('segments'):
            raise ValueError('Linked rollout requires history segments; re-export with chain support')
        if meta(data).get('id') != sid or s['thread'].get('id') != sid or set(s['thread'])-FIELDS:
            raise ValueError(f'Invalid metadata: {sid}')
        if relative(s['cwd'],old) is None:
            raise ValueError(f'Session outside backup project: {sid}')
        for line in data.splitlines(): json.loads(line)
        mapping = {}
        for asset in s.get('assets',[]):
            if asset['status'] != 'bundled': continue
            source = checked_file(backup,asset)
            target_asset = home/'attachments'/'restored'/sid/source.name
            if not target_asset.resolve().is_relative_to(home.resolve()):
                raise ValueError('Attachment destination escapes Codex home')
            if target_asset.exists(): raise ValueError(f'Existing attachment conflict: {target_asset}')
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != asset['sha256']: raise ValueError('Attachment checksum mismatch')
            asset_files[target_asset] = content
            mapping[asset['reference']] = target_asset.as_uri() if asset['reference'].startswith('file:') else str(target_asset)
        asset_mappings[sid] = mapping
        offsets = None
        if new or mapping: data,offsets = rewrite(data,old,new,mapping)
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
                        if entry.get('item_json') and asset_mappings[sid]:
                            entry['item_json'] = json.dumps(replace_references(json.loads(entry['item_json']),asset_mappings[sid]))
                        for k,v in list(entry.items()):
                            if offsets is not None and k.endswith('byte_offset') and v is not None:
                                if v not in offsets: raise ValueError(f'History offset is not a rollout boundary: {sid}')
                                entry[k]=offsets[v]
                        insert(db,schema,table,entry)
        for target,content in asset_files.items():
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            created.append(target)
            with os.fdopen(fd,'wb') as f: f.write(content); f.flush(); os.fsync(f.fileno())
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
    return {'restored':len(prepared),'codex_home':str(home),'restored_asset_files':len(asset_files),
            'unresolved_assets':sum(a['status']=='unresolved' for s in manifest['sessions'] for a in s.get('assets',[]))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME','~/.codex')).expanduser())
    sub=parser.add_subparsers(dest='command',required=True)
    exp=sub.add_parser('export');exp.add_argument('project_path');exp.add_argument('destination',type=Path)
    exp.add_argument('--include-attachments',action='store_true')
    exp.add_argument('--compact',action='store_true',help='Deduplicate embedded images and gzip each project backup')
    check=sub.add_parser('verify',help='Check a backup offline without restoring it');check.add_argument('backup',type=Path)
    bulk=sub.add_parser('export-all', help='Export all sessions grouped into separate project backups')
    bulk.add_argument('destination',type=Path);bulk.add_argument('--dry-run',action='store_true')
    bulk.add_argument('--include-attachments',action='store_true')
    bulk.add_argument('--compact',action='store_true',help='Create independently restorable compact project backups')
    res=sub.add_parser('restore');res.add_argument('backup',type=Path);res.add_argument('new_project_path',nargs='?')
    args=parser.parse_args()
    try:
        home=args.codex_home.expanduser().resolve()
        if args.command == 'verify':
            result=verify(args.backup.expanduser().resolve())
        elif args.command == 'export-all':
            result=export_all(home,args.destination.expanduser().resolve(),args.dry_run,args.include_attachments,args.compact)
        elif args.command == 'export':
            result=export(home,args.project_path,args.destination.expanduser().resolve(),include_attachments=args.include_attachments,compact=args.compact)
        else:
            result=restore(home,args.backup.expanduser().resolve(),args.new_project_path)
        print(dumps(result),end='')
        if result.get('valid') is False:
            return 1
        if result.get('complete') is False:
            return 2
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as e:
        print(f'error: {e}',file=sys.stderr);return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
