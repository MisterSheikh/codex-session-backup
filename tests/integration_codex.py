"""Opt-in: python3 tests/integration_codex.py [source-codex-home]. Copies one session; never resumes the original."""
import sys,os,json,sqlite3,pathlib,tempfile,subprocess,select,shutil
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import codex_sessions as cs
temporary=tempfile.TemporaryDirectory(prefix='backup-integration-');root=pathlib.Path(temporary.name);source=root/'source';target=root/'target';source.mkdir();target.mkdir()
live=pathlib.Path(sys.argv[1]).expanduser().resolve() if len(sys.argv)>1 else pathlib.Path.home()/'.codex'
c=cs.connect(live/'state_5.sqlite')
r=dict(c.execute("select * from threads where source='cli' and archived=0 order by created_at desc limit 1").fetchone());sid=r['id']
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
    for row in cs.rows(orig,t,sid,'id' if t=='threads' else 'thread_id'):
     if t=='threads':row['rollout_path']=str(source/'sessions'/pathlib.Path(r['rollout_path']).name)
     cs.insert(db,'main',t,row)
  db.commit();db.close()
(source/'sessions').mkdir();shutil.copyfile(r['rollout_path'],source/'sessions'/pathlib.Path(r['rollout_path']).name)
print('Testing one copied CLI session in disposable state')
print(cs.export(source,r['cwd'],root/'backup'))
print(cs.restore(target,root/'backup',str(root/'moved')))
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
 for m,p in [('thread/list',{'modelProviders':[],'useStateDbOnly':False}),('thread/turns/list',{'threadId':sid,'limit':2}),('thread/read',{'threadId':sid}),('thread/resume',{'threadId':sid,'excludeTurns':True})]:
  out=rpc(m,p)
  print(m,'count',len(out.get('data',[])),'cwd',out.get('thread',{}).get('cwd'))
  if m=='thread/list':assert any(x['id']==sid for x in out['data'])
  if m=='thread/turns/list':assert len(out['data'])>0
  if m in ('thread/read','thread/resume'):assert out['thread']['cwd']==str(root/'moved')
finally:
 proc.terminate();proc.wait();temporary.cleanup()
