"""Offline backup verification; no Codex processes, database writes or network."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import uuid
import session_chains as chains
from session_assets import asset_references


def contained(root, name):
    if not isinstance(name,str) or not name or Path(name).is_absolute() or PureWindowsPath(name).is_absolute() or '..' in name.replace('\\','/').split('/'):
        raise ValueError('Path escapes backup or is not relative')
    path = (root/name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path escapes backup')
    return path


def checked_file(root, entry):
    path = contained(root,entry['file'])
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            digest.update(chunk)
    if digest.hexdigest() != entry['sha256']:
        raise ValueError(f'Checksum mismatch: {entry["file"]}')
    if 'size' in entry and entry['size'] != path.stat().st_size:
        raise ValueError(f'Size mismatch: {entry["file"]}')
    return path


def verify(backup, _visited=None):
    from codex_sessions import FIELDS, HISTORY, RELATED, relative
    backup = Path(backup).resolve()
    visited = set() if _visited is None else _visited
    result = {'backup':str(backup),'valid':True,'complete':True,'sessions':0,'assets':0,
              'errors':[],'warnings':[], 'resume_tested':False}
    errors, warnings = result['errors'], result['warnings']
    def finish():
        result['valid'] = not errors
        result['complete'] = not errors and not warnings
        return result
    try:
        if backup in visited:
            raise ValueError('Repeated/cyclic project backup')
        visited.add(backup)
        if (backup/'manifest.json').exists() and (backup/'index.json').exists():
            raise ValueError('Ambiguous backup: both manifest.json and index.json exist')
        collection = (backup/'index.json').exists()
        doc = json.loads((backup/('index.json' if collection else 'manifest.json')).read_text())
        if not isinstance(doc,dict):
            raise ValueError('Manifest/index must be an object')
        if collection:
            if doc.get('format') != 'codex-project-session-collection' or doc.get('version') != 1:
                raise ValueError('Unsupported collection format/version')
            children, ids = [], set()
            for project in doc['projects']:
                if project['status'] != 'exported':
                    errors.append(f'Project not exported: {project["directory"]}')
                    continue
                try:
                    child = contained(backup,project['directory'])
                    report = verify(child,visited)
                    children.append(report)
                    errors.extend(f'{project["directory"]}: {e}' for e in report['errors'])
                    warnings.extend(f'{project["directory"]}: {w}' for w in report['warnings'])
                    result['sessions'] += report['sessions']; result['assets'] += report['assets']
                    manifest = json.loads((child/'manifest.json').read_text())
                    actual = [s['id'] for s in manifest['sessions']]
                    expected = [s['id'] for s in project['sessions']]
                    if sorted(actual) != sorted(expected) or len(actual) != project['exported'] or manifest['project_path'] != project['project_path']:
                        raise ValueError('Collection assignment/count does not match project manifest')
                    if ids.intersection(actual):
                        raise ValueError('Session ID occurs in multiple project backups')
                    ids.update(actual)
                except (OSError,ValueError,KeyError,TypeError,AttributeError) as e:
                    errors.append(f'{project.get("directory")}: {e}')
            result['projects'] = len(children)
            if result['sessions'] != doc['exported']:
                errors.append('Collection session total does not match index')
            for entry in doc['unassigned']:
                try:
                    path = checked_file(backup,entry)
                    with path.open('rb') as f:
                        if not any(line.strip() for line in f): raise ValueError('Empty unassigned rollout')
                    with path.open('rb') as f:
                        for line in f:
                            if not isinstance(json.loads(line),dict):raise ValueError('Non-object JSONL record')
                except (OSError,ValueError,KeyError,TypeError) as e:
                    errors.append(f'Unassigned rollout: {e}')
            if doc['unassigned']:
                warnings.append(f'{len(doc["unassigned"])} raw rollouts need manual assignment; not normal restorable backups')
            return finish()
        if doc.get('format') != 'codex-project-sessions' or doc.get('version') not in (1,2,3):
            raise ValueError('Unsupported backup format/version')
        if doc['version']>=2 and not isinstance(doc.get('source_codex_home'),str):
            raise ValueError('Missing source_codex_home')
        ids, files = set(), set()
        if not isinstance(doc['sessions'],list) or not isinstance(doc['project_path'],str):
            raise ValueError('Invalid sessions/project_path')
        for session in doc['sessions']:
            label = session.get('id','unknown') if isinstance(session,dict) else 'unknown'
            try:
                sid = session['id']
                if str(uuid.UUID(sid)) != sid or sid in ids:raise ValueError('Invalid/duplicate session ID')
                ids.add(sid)
                result['sessions']+=1
                if session['file'] in files:raise ValueError('Shared rollout file between sessions')
                files.add(session['file'])
                for required in ('cwd','original_rollout_path','timestamp','archived','thread','related','history'):
                    if required not in session:raise ValueError(f'Missing session field: {required}')
                if not isinstance(session['cwd'],str) or not isinstance(session['original_rollout_path'],str) or type(session['archived']) is not bool:
                    raise ValueError('Invalid session path/archive metadata')
                thread = session['thread']
                if thread.get('id') != sid or set(thread)-FIELDS:raise ValueError('Invalid thread metadata')
                if relative(session['cwd'],doc['project_path']) is None:raise ValueError('Session outside project')
                if thread.get('cwd') != session['cwd']:raise ValueError('Manifest and thread cwd disagree')
                path = checked_file(backup,session)
                boundaries, position, refs, first = {0}, 0, set(), None
                with path.open('rb') as f:
                    for line in f:
                        record = json.loads(line)
                        if not isinstance(record,dict):raise ValueError('Non-object JSONL record')
                        if first is None:first = record['payload'] if record.get('type')=='session_meta' else record
                        position += len(line); boundaries.add(position)
                        found,_ = asset_references(record,doc.get('source_codex_home'))
                        refs.update(found)
                if first is None or first.get('id') != sid:raise ValueError('Rollout ID mismatch or empty rollout')
                if first.get('cwd') and first['cwd'] != session['cwd']:raise ValueError('Rollout cwd disagrees with manifest')
                if first.get('history_base') and not session.get('segments'):
                    raise ValueError('Linked rollout requires history segments; re-export with chain support')
                if session.get('segments'):
                    if doc['version']<3:raise ValueError('History segments require backup version 3')
                    segments=[(sg,checked_file(backup,sg).read_bytes()) for sg in session['segments']]
                    chain=chains.validate(segments,session['active_thread_id'])
                    if segments[-1][0]['file']!=session['file'] or segments[-1][0]['sha256']!=session['sha256']:
                        raise ValueError('Active rollout does not match chain tip')
                    if any(session['history'].values()) or any(session['related'].values()):raise ValueError('Chain history must be stored per segment')
                    for sg,data in segments:
                        points=chain[sg['thread_id']][1]
                        for record in (json.loads(line) for line in data.splitlines()):
                            found,_=asset_references(record,doc.get('source_codex_home'));refs.update(found)
                        for group,allowed in [('related',RELATED),('history',HISTORY)]:
                            if set(sg[group])!=set(allowed):raise ValueError('Missing/unknown segment tables')
                            for entries in sg[group].values():
                                for row in entries:
                                    if row.get('thread_id')!=sg['thread_id']:raise ValueError('Cross-segment database row')
                                    for field,value in row.items():
                                        if field.endswith('byte_offset') and value is not None and (type(value) is not int or value not in points.values()):raise ValueError('Segment history offset is not a rollout boundary')
                                    if row.get('item_json'):
                                        found,_=asset_references(json.loads(row['item_json']),doc.get('source_codex_home'));refs.update(found)
                for group,allowed in [('related',RELATED),('history',HISTORY)]:
                    tables=session[group]
                    if set(tables)!=set(allowed):raise ValueError(f'Missing/unknown {group} tables')
                    for entries in tables.values():
                        if not isinstance(entries,list):raise ValueError('Table rows must be a list')
                        for row in entries:
                            if row.get('thread_id')!=sid:raise ValueError('Cross-session database row')
                            for key,value in row.items():
                                if key.endswith('byte_offset') and value is not None and (type(value) is not int or value not in boundaries):
                                    raise ValueError('History offset is not a rollout boundary')
                            if row.get('item_json'):
                                found,_=asset_references(json.loads(row['item_json']),doc.get('source_codex_home'));refs.update(found)
                entries=session.get('assets',[])
                asset_refs=set()
                for entry in entries:
                    ref=entry['reference']
                    if not isinstance(ref,str) or ref in asset_refs:raise ValueError('Invalid/duplicate asset reference')
                    asset_refs.add(ref)
                    if entry['status']=='bundled':
                        if not entry['file'].startswith(f'assets/{sid}/'):raise ValueError('Asset outside session asset directory')
                        checked_file(backup,entry);result['assets']+=1
                    elif entry['status']=='unresolved':
                        warnings.append(f'{sid}: unresolved attachment: {entry.get("reason","not bundled")}')
                    else:raise ValueError('Unknown asset status')
                missing=refs-asset_refs
                if missing:warnings.append(f'{sid}: {len(missing)} external media references are not inventoried/bundled')
                if doc['version']>=2 and not isinstance(session.get('assets'),list):raise ValueError('Missing asset inventory')
            except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError) as e:
                errors.append(f'{label}: {e}')
        if doc['version']==1:
            warnings.append('Version 1 has no attachment inventory; attachment completeness is unknown')
        if doc.get('skipped_without_cwd',0):warnings.append('Export skipped sessions without a recorded cwd')
    except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError) as e:
        errors.append(str(e))
    return finish()
