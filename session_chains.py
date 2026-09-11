"""Native rollout history chains: physical thread IDs and explicit base links."""
import json
import re
from pathlib import Path
import uuid

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def thread_id(path, metadata):
    match = re.search(r'('+UUID+r')(?:_('+UUID+r'))?\.jsonl$',Path(path).name)
    return (match.group(2) or match.group(1)) if match else metadata['id']


def metadata(data):
    lines=data.splitlines()
    if not lines:raise ValueError('Empty rollout')
    first=json.loads(lines[0])
    if not isinstance(first,dict):raise ValueError('Non-object session metadata')
    result=first.get('payload') if first.get('type')=='session_meta' else first
    if not isinstance(result,dict):raise ValueError('Invalid session metadata payload')
    return result


def catalog(paths):
    found={}
    for path in paths:
        try:
            with path.open('rb') as f:m=metadata(f.readline())
            if 'id' in m:found.setdefault(thread_id(path,m),[]).append(path)
        except (OSError,ValueError,KeyError,TypeError):
            continue  # Discovery reports malformed files separately; dependencies fail resolution.
    return found


def resolve(path, candidates):
    """Return base-first paths. Never guess between duplicate physical IDs."""
    ordered=[];visiting=set()
    def visit(p):
        with p.open('rb') as f:m=metadata(f.readline())
        key=thread_id(p,m)
        if key in visiting:raise ValueError(f'Cyclic history_base: {key}')
        visiting.add(key)
        base=m.get('history_base')
        if base:
            options=candidates.get(base['thread_id'],[])
            if len(options)!=1:raise ValueError(f'Missing/ambiguous history_base rollout: {base["thread_id"]}')
            visit(options[0])
        ordered.append(p);visiting.remove(key)
    visit(path)
    return ordered


def boundaries(data):
    """Map explicit native ordinals to local file byte boundaries."""
    result={};offset=0;previous=None
    for index,line in enumerate(data.splitlines(keepends=True)):
        record=json.loads(line)
        if not isinstance(record,dict):raise ValueError('Non-object rollout record')
        ordinal=record.get('ordinal',index)
        if type(ordinal) is not int or ordinal<0 or (previous is not None and ordinal!=previous+1):
            raise ValueError('Invalid/non-contiguous rollout ordinals')
        result[ordinal]=offset;offset+=len(line);previous=ordinal
    if previous is None:raise ValueError('Empty rollout')
    result[previous+1]=offset
    return result


def validate(segments, active):
    """Validate a base-first segment list and its exact cutoff links."""
    seen={};ancestors=[]
    for segment,data in segments:
        key=segment['thread_id']
        if str(uuid.UUID(key))!=key or key in seen:raise ValueError('Invalid/duplicate physical thread ID')
        m=metadata(data)
        if m.get('id')!=segment['session_id'] or str(uuid.UUID(segment['session_id']))!=segment['session_id']:raise ValueError('Segment session ID mismatch')
        if thread_id(segment['original_rollout_path'],m)!=key:raise ValueError('Segment physical ID does not match original filename')
        points=boundaries(data)
        base=m.get('history_base')
        if base:
            parent=seen.get(base['thread_id'])
            if parent is None:raise ValueError('Missing/unordered history_base segment')
            if type(base.get('end_ordinal_exclusive')) is not int or type(base.get('end_byte_offset')) is not int:
                raise ValueError('Invalid history_base cutoff')
            if parent[1].get(base['end_ordinal_exclusive'])!=base['end_byte_offset']:
                raise ValueError('history_base cutoff does not match parent ordinal/byte boundary')
            if min(points)!=base['end_ordinal_exclusive']:
                raise ValueError('Continuation ordinal does not start at history_base cutoff')
            ancestors.append(base['thread_id'])
        elif seen:raise ValueError('Unlinked extra segment')
        seen[key]=(segment,points)
    if not segments or segments[-1][0]['thread_id']!=active:raise ValueError('Active segment is not chain tip')
    if ancestors!=[s['thread_id'] for s,d in segments[:-1]]:raise ValueError('Segments are not one history chain')
    return seen


def restore_native(home, backup, manifest, new_project):
    """Restore a collection of native segments once, including shared ancestors."""
    import os
    import sqlite3
    from datetime import datetime
    import codex_sessions as cs
    from backup_verify import checked_file
    from session_assets import replace_references
    state=cs.database(home,'state_*.sqlite','threads');history=cs.database(home,'thread_history_*.sqlite','thread_turns')
    old=manifest['project_path'];new=str(Path(new_project).expanduser().resolve()) if new_project else None
    physical={};session_rows={};assets={};mapping={};expected_ids=set()
    for session in manifest['sessions']:
        for asset in session.get('assets',[]):
            if asset['status']!='bundled':continue
            source=checked_file(backup,asset);target=home/'attachments'/'restored'/'shared'/source.name
            if not target.resolve().is_relative_to(home.resolve()):raise ValueError('Attachment destination escapes Codex home')
            content=source.read_bytes()
            if target in assets and assets[target]!=content:raise ValueError('Conflicting shared attachment')
            assets[target]=content
            mapping[asset['reference']]=target.as_uri() if asset['reference'].startswith('file:') else str(target)
        segments=session.get('segments') or [dict(thread_id=session['id'],session_id=session['id'],**{k:session[k] for k in ('file','sha256','original_rollout_path','history','related')})]
        for segment in segments:
            data=checked_file(backup,segment).read_bytes();key=segment['thread_id']
            if key in physical:
                existing=physical[key]
                if existing['data']!=data or existing['segment']['history']!=segment['history'] or existing['segment']['related']!=segment['related']:
                    raise ValueError(f'Conflicting shared history segment: {key}')
                continue
            m=metadata(data)
            dt=datetime.fromisoformat(m['timestamp'].replace('Z','+00:00'))
            suffix=m['id'] if m['id']==key else m['id']+'_'+key
            # Dependencies must be discoverable by their native UUID filenames.
            folder=home/'archived_sessions' if segment.get('archived',session['archived']) else home/'sessions'/dt.strftime('%Y/%m/%d')
            target=folder/f'rollout-{dt.strftime("%Y-%m-%dT%H-%M-%S")}-{suffix}.jsonl'
            physical[key]={'data':data,'segment':segment,'target':target,'meta':m}
            expected_ids.update((key,m['id']))
        active=session.get('active_thread_id',session['id'])
        row=dict(session['thread']);row['rollout_path']=str(physical[active]['target'])
        row['cwd']=str(Path(new).joinpath(*cs.relative(session['cwd'],old))) if new else session['cwd']
        if new and row.get('sandbox_policy'):row['sandbox_policy']=json.dumps(cs.remap(json.loads(row['sandbox_policy']),old,new))
        session_rows[session['id']]=row;expected_ids.add(session['id'])
    # Build parent-first, rewriting base cutoffs in the child after its parent.
    pending=set(physical);rewritten={}
    while pending:
        progressed=False
        for key in list(pending):
            item=physical[key];base=item['meta'].get('history_base')
            if base and base['thread_id'] not in rewritten:continue
            data=item['data']
            if base:
                lines=data.splitlines(keepends=True);first=json.loads(lines[0])
                first['payload']['history_base']['end_byte_offset']=rewritten[base['thread_id']]['offsets'][base['end_byte_offset']]
                lines[0]=(json.dumps(first,ensure_ascii=False)+'\n').encode()
                # Compose original -> header-edited -> fully rewritten byte maps.
                adjusted=b''.join(lines);delta=len(lines[0])-len(data.splitlines(keepends=True)[0])
                changed,offsets=cs.rewrite(adjusted,old,new,mapping)
                original_bounds=boundaries(data)
                offsets={pos:offsets[pos+delta if pos else 0] for pos in original_bounds.values()}
            else:
                changed,offsets=cs.rewrite(data,old,new,mapping)
            rewritten[key]={'data':changed,'offsets':offsets};pending.remove(key);progressed=True
        if not progressed:raise ValueError('Missing/cyclic history base during restore')
    for path in cs.rollout_files(home):
        m=cs.file_meta(path)
        if m.get('id') in expected_ids or thread_id(path,m) in expected_ids:raise ValueError(f'Existing rollout conflict: {path}')
    index=home/'session_index.jsonl'
    if index.exists():
        for line in index.read_text().splitlines():
            if json.loads(line).get('id') in expected_ids:raise ValueError('Existing session_index conflict')
    files={item['target']:rewritten[key]['data'] for key,item in physical.items()};files.update(assets)
    for path in files:
        if path.exists():raise ValueError(f'Existing file conflict: {path}')
        if not path.resolve().is_relative_to(home.resolve()):raise ValueError('Destination escapes Codex home')
    created=[];db=sqlite3.connect(state)
    try:
        db.execute('PRAGMA foreign_keys=ON');db.execute('ATTACH DATABASE ? AS hist',(str(history),));db.execute('BEGIN IMMEDIATE')
        for key in expected_ids:
            for schema,tables in [('main',('threads',)+cs.RELATED),('hist',cs.HISTORY)]:
                for table in tables:
                    column='id' if table=='threads' else 'thread_id'
                    if db.execute(f'SELECT 1 FROM {schema}.{table} WHERE {column}=?',(key,)).fetchone():raise ValueError(f'Existing {table} conflict: {key}')
        for row in session_rows.values():cs.insert(db,'main','threads',row)
        for key,item in physical.items():
            for schema,group in [('main','related'),('hist','history')]:
                for table,entries in item['segment'][group].items():
                    for entry in entries:
                        entry=dict(entry)
                        for field,value in list(entry.items()):
                            if field.endswith('byte_offset') and value is not None:entry[field]=rewritten[key]['offsets'][value]
                        if entry.get('item_json'):entry['item_json']=json.dumps(replace_references(json.loads(entry['item_json']),mapping))
                        cs.insert(db,schema,table,entry)
        for path,data in files.items():
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);created.append(path)
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        db.commit()
    except BaseException:
        db.rollback()
        for path in created:path.unlink()
        raise
    finally:db.close()
    return {'restored':len(session_rows),'restored_segments':len(physical),'restored_asset_files':len(assets),'codex_home':str(home)}
