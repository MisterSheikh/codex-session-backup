"""Opt-in: python3 tests/integration_codex.py [source-codex-home]. Copies one session; never resumes the original."""
import sys,os,json,sqlite3,pathlib,tempfile,subprocess,select,shutil,base64
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import codex_sessions as cs
temporary=tempfile.TemporaryDirectory(prefix='backup-integration-');root=pathlib.Path(temporary.name);source=root/'source';target=root/'target';source.mkdir();target.mkdir()
live=pathlib.Path(sys.argv[1]).expanduser().resolve() if len(sys.argv)>1 else pathlib.Path.home()/'.codex'
c=cs.connect(live/'state_5.sqlite')
r=dict(c.execute('SELECT * FROM threads WHERE id=?',(sys.argv[2],)).fetchone()) if len(sys.argv)>2 else dict(c.execute("select * from threads where source='cli' and archived=0 order by created_at desc limit 1").fetchone())
sid=r['id']
chain_paths=cs.chains.resolve(pathlib.Path(r['rollout_path']),cs.chains.catalog(list(cs.rollout_files(live))))
physical_ids=[cs.chains.thread_id(p,cs.file_meta(p)) for p in chain_paths]
for name,tables in [('state_5.sqlite',('threads',)+cs.RELATED),('thread_history_1.sqlite',cs.HISTORY)]:
 orig=cs.connect(live/name)
 for dest in [source,target]:
  db=sqlite3.connect(dest/name)
  for (sql,) in orig.execute("select sql from sqlite_master where type='table'"):
   db.execute(sql)
  for migration in orig.execute('SELECT * FROM _sqlx_migrations'):
   db.execute('INSERT INTO _sqlx_migrations VALUES (?,?,?,?,?,?)',tuple(migration))
  if dest==source:
   for t in tables:
    for row in (cs.rows(orig,t,sid,'id') if t=='threads' else [row for key in physical_ids for row in cs.rows(orig,t,key)]):
     if t=='threads':row['rollout_path']=str(source/'sessions'/pathlib.Path(r['rollout_path']).name)
     cs.insert(db,'main',t,row)
  db.commit();db.close()
(source/'sessions').mkdir()
for path in chain_paths:shutil.copyfile(path,source/'sessions'/path.name)
# Add a tiny synthetic image to a copied history item, never to live state.
fixture_asset=source/'attachments'/'fixture'/'pixel.png';fixture_asset.parent.mkdir(parents=True)
fixture_asset.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
with sqlite3.connect(source/'thread_history_1.sqlite') as hd:
 item=hd.execute("SELECT item_id,item_json FROM thread_items WHERE thread_id=? AND item_type='userMessage' LIMIT 1",(physical_ids[0],)).fetchone()
 if item is None:raise RuntimeError('Selected fixture session has no user message')
 body=json.loads(item[1]);body['content'].append({'type':'localImage','path':str(fixture_asset)})
 hd.execute('UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id=?',(json.dumps(body),physical_ids[0],item[0]))
print('Testing one copied CLI session and a synthetic attachment in disposable state')
print(cs.export(source,r['cwd'],root/'backup',include_attachments=True,selected_paths=[source/'sessions'/pathlib.Path(r['rollout_path']).name]))
assert cs.verify(root/'backup')['valid']
fixture_asset.unlink()
print(cs.restore(target,root/'backup',str(root/'moved')))
expected_turns=set()
for i,path in enumerate(chain_paths):
 cutoff=cs.file_meta(chain_paths[i+1])['history_base']['end_ordinal_exclusive'] if i+1<len(chain_paths) else None
 for number,line in enumerate(path.open()):
  record=json.loads(line)
  if cutoff is not None and record.get('ordinal',number)>=cutoff:break
  if record.get('type')=='event_msg' and record.get('payload',{}).get('type')=='task_started':expected_turns.add(record['payload']['turn_id'])
proc=subprocess.Popen(['codex','app-server','--stdio'],env={**os.environ,'CODEX_HOME':str(target)},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=open(root/'server.log','w'),text=True)
def rpc(method,params):
 rpc.i+=1;proc.stdin.write(json.dumps({'id':rpc.i,'method':method,'params':params})+'\n');proc.stdin.flush()
 while select.select([proc.stdout],[],[],20)[0]:
  x=json.loads(proc.stdout.readline())
  if x.get('id')==rpc.i:
   if 'error' in x:raise RuntimeError(x['error'])
   return x['result']
 raise RuntimeError('timeout')
rpc.i=0
try:
 rpc('initialize',{'clientInfo':{'name':'backup_test','version':'1'},'capabilities':{'experimentalApi':True}})
 for m,p in [('thread/list',{'modelProviders':[],'useStateDbOnly':False}),('thread/turns/list',{'threadId':sid,'limit':100,'itemsView':'full'}),('thread/read',{'threadId':sid}),('thread/resume',{'threadId':sid,'excludeTurns':True})]:
  out=rpc(m,p)
  print(m,'count',len(out.get('data',[])),'cwd',out.get('thread',{}).get('cwd'))
  if m=='thread/list':assert any(x['id']==sid for x in out['data'])
  if m=='thread/turns/list':
   assert {turn['id'] for turn in out['data']}==expected_turns, (len(out['data']),len(expected_turns))
   assert str(target/'attachments'/'restored') in json.dumps(out)
   assert next((target/'attachments'/'restored').rglob('*.png')).is_file()
  if m in ('thread/read','thread/resume'):assert out['thread']['cwd']==str(root/'moved')
finally:
 proc.terminate();proc.wait();temporary.cleanup()
