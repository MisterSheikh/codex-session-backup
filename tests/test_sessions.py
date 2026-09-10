import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
import codex_sessions as cs


def home(path):
    path.mkdir()
    with sqlite3.connect(path/'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cwd TEXT NOT NULL, created_at INTEGER, updated_at INTEGER, title TEXT, archived INTEGER, history_mode TEXT)')
        db.execute('CREATE TABLE thread_dynamic_tools (thread_id TEXT, name TEXT)')
    with sqlite3.connect(path/'thread_history_1.sqlite') as db:
        db.execute('CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_byte_offset INTEGER, rollout_end_byte_offset INTEGER)')
        db.execute('CREATE TABLE thread_items (thread_id TEXT, item_json TEXT)')
        db.execute('CREATE TABLE thread_realtime_items (thread_id TEXT, item_json TEXT)')
        db.execute('CREATE TABLE thread_history_projection_state (thread_id TEXT, next_rollout_byte_offset INTEGER)')


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.src=self.root/'source';self.dst=self.root/'target'
        home(self.src);home(self.dst)
        self.project=str(self.root/'project');self.sid=str(uuid.uuid4())
        self.add_session(self.sid,self.project+'/child')
        self.add_session(str(uuid.uuid4()),self.project+'-unrelated')
        (self.src/'auth.json').write_text('DO NOT EXPORT')
        self.backup=self.root/'backup'

    def add_session(self,sid,cwd):
        path=self.src/'sessions'/f'rollout-{sid}.jsonl';path.parent.mkdir(exist_ok=True)
        records=[{'type':'session_meta','payload':{'id':sid,'cwd':cwd,'timestamp':'2026-09-01T12:00:00Z'}},
                 {'type':'turn_context','payload':{'cwd':cwd}},
                 {'type':'response_item','payload':{'text':'literal '+cwd}}]
        data=b''.join((json.dumps(r)+'\n').encode() for r in records);path.write_bytes(data)
        with sqlite3.connect(self.src/'state_5.sqlite') as db:
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)',(sid,str(path),cwd,1788264000,1788264001,'A title',0,'paginated'))
        with sqlite3.connect(self.src/'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',(sid,'turn',0,len(data)))
            db.execute('INSERT INTO thread_history_projection_state VALUES (?,?)',(sid,len(data)))

    def export(self):
        return cs.export(self.src,self.project,self.backup)

    def test_roundtrip_remap_history_and_scope(self):
        self.assertEqual(self.export()['exported'],1)
        self.assertEqual({p.name for p in self.backup.iterdir()},{'manifest.json','rollouts'})
        new=str(self.root/'a-much-longer-new-project')
        cs.restore(self.dst,self.backup,new)
        with cs.connect(self.dst/'state_5.sqlite') as db:
            row=dict(db.execute('SELECT * FROM threads').fetchone())
        self.assertEqual(row['cwd'],new+'/child');self.assertEqual(row['title'],'A title')
        data=Path(row['rollout_path']).read_bytes();records=[json.loads(l) for l in data.splitlines()]
        self.assertEqual(records[1]['payload']['cwd'],new+'/child')
        self.assertEqual(records[2]['payload']['text'],'literal '+self.project+'/child')
        with cs.connect(self.dst/'thread_history_1.sqlite') as db:
            self.assertEqual(db.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state').fetchone()[0],len(data))

    def test_no_move_preserves_bytes_and_conflicts(self):
        self.export();cs.restore(self.dst,self.backup)
        data=next((self.backup/'rollouts').glob('*')).read_bytes()
        self.assertEqual(next((self.dst/'sessions').rglob('*.jsonl')).read_bytes(),data)
        with self.assertRaisesRegex(ValueError,'conflict'):cs.restore(self.dst,self.backup)

    def test_tampered_rollout(self):
        self.export();next((self.backup/'rollouts').glob('*')).write_text('{}\n')
        with self.assertRaisesRegex(ValueError,'Checksum'):cs.restore(self.dst,self.backup)
        self.assertFalse((self.dst/'sessions').exists())

    def test_path_traversal(self):
        self.export();p=self.backup/'manifest.json';m=json.loads(p.read_text());m['sessions'][0]['file']='../auth.json';p.write_text(cs.dumps(m))
        with self.assertRaisesRegex(ValueError,'escapes'):cs.restore(self.dst,self.backup)

    def test_orphan_history_conflict_rolls_back(self):
        self.export()
        with sqlite3.connect(self.dst/'thread_history_1.sqlite') as db:db.execute('INSERT INTO thread_items VALUES (?,?)',(self.sid,'{}'))
        with self.assertRaisesRegex(ValueError,'conflict'):cs.restore(self.dst,self.backup)
        with cs.connect(self.dst/'state_5.sqlite') as db:self.assertEqual(db.execute('SELECT count(*) FROM threads').fetchone()[0],0)
        self.assertFalse((self.dst/'sessions').exists())

    def test_schema_failure_rolls_back(self):
        self.export()
        with sqlite3.connect(self.dst/'state_5.sqlite') as db:db.execute('ALTER TABLE threads ADD COLUMN future TEXT NOT NULL')
        with self.assertRaises(sqlite3.IntegrityError):cs.restore(self.dst,self.backup)
        self.assertFalse((self.dst/'sessions').exists())

    def test_archived_session_stays_archived(self):
        with sqlite3.connect(self.src/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET archived=1 WHERE id=?',(self.sid,))
        self.export();cs.restore(self.dst,self.backup)
        self.assertEqual(len(list((self.dst/'archived_sessions').glob('*.jsonl'))),1)

    def test_index_only_conflict(self):
        self.export()
        (self.dst/'session_index.jsonl').write_text(json.dumps({'id':self.sid,'thread_name':'Existing'})+'\n')
        with self.assertRaisesRegex(ValueError,'conflict'):cs.restore(self.dst,self.backup)

    def test_structured_paths_and_literal_tool_arguments(self):
        records=[{'type':'turn_context','payload':{'workspace_roots':['/old/sub'],
                 'sandbox_policy':{'writable_roots':['/old','/elsewhere']},'permission':{'path':'/old/sub'}}},
                 {'type':'response_item','payload':{'path':'/old/sub','cwd':'/old'}}]
        data=b''.join((json.dumps(r)+'\n').encode() for r in records)
        result,_=cs.rewrite(data,'/old','/new')
        out=[json.loads(l) for l in result.splitlines()]
        self.assertEqual(out[0]['payload']['workspace_roots'],['/new/sub'])
        self.assertEqual(out[0]['payload']['sandbox_policy']['writable_roots'],['/new','/elsewhere'])
        self.assertEqual(out[1],records[1])

    def test_bulk_groups_without_overlapping_exports(self):
        # A home-level cwd must not absorb the nested project sessions.
        home_sid=str(uuid.uuid4());self.add_session(home_sid,str(self.root))
        Path(self.project).mkdir();(Path(self.project)/'.git').mkdir()
        out=self.root/'collection'
        with patch.object(cs, 'project_root', side_effect=lambda cwd, registered: (self.project if cs.relative(cwd,self.project) is not None else cwd, 'test-root')):
            result=cs.export_all(self.src,out)
        self.assertTrue(result['complete']);self.assertEqual(result['exported'],3)
        projects={p['project_path']:p for p in result['projects']}
        self.assertEqual(len(projects[self.project]['sessions']),1)
        self.assertEqual(len(projects[str(self.root)]['sessions']),1)
        cs.restore(self.dst,out/projects[self.project]['directory'],str(self.root/'moved'))
        with cs.connect(self.dst/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT cwd FROM threads').fetchone()[0],str(self.root/'moved/child'))

    def test_bulk_dry_run_and_colliding_names(self):
        self.add_session(str(uuid.uuid4()),str(self.root/'elsewhere/child'))
        out=self.root/'collection'
        with patch.object(cs, 'project_root', side_effect=lambda cwd, registered: (cwd, 'recorded-cwd')):
            result=cs.export_all(self.src,out,dry_run=True)
        self.assertFalse(out.exists())
        names=[p['directory'] for p in result['projects']]
        self.assertEqual(len(names),len(set(names)))
        self.assertTrue(any(n.startswith('child-') for n in names))

    def test_bulk_records_failure_and_continues(self):
        path=self.src/'sessions'/f'rollout-{self.sid}.jsonl'
        with path.open('ab') as f:f.write(b'broken json\n')
        with patch.object(cs, 'project_root', side_effect=lambda cwd, registered: (cwd, 'recorded-cwd')):
            result=cs.export_all(self.src,self.root/'collection')
        self.assertFalse(result['complete']);self.assertEqual(result['exported'],1)
        self.assertEqual(sum(p['status']=='failed' for p in result['projects']),1)

    def test_bulk_preserves_unassigned_raw_data(self):
        raw=self.src/'sessions/ancient.jsonl';raw.write_text('{"id":"ancient"}\n')
        result=cs.export_all(self.src,self.root/'collection')
        self.assertFalse(result['complete']);self.assertEqual(result['preserved_unassigned'],1)
        entry=result['unassigned'][0]
        self.assertEqual((self.root/'collection'/entry['file']).read_bytes(),raw.read_bytes())

    def test_bulk_uses_indexed_duplicate_and_preserves_other(self):
        original=self.src/'sessions'/f'rollout-{self.sid}.jsonl'
        extra=self.src/'sessions/older-copy.jsonl';extra.write_bytes(original.read_bytes())
        result=cs.export_all(self.src,self.root/'collection')
        self.assertEqual(result['exported'],2)
        self.assertEqual(result['preserved_unassigned'],1)
        self.assertEqual(result['unassigned'][0]['indexed_rollout_path'],str(original))

    def test_remap_thread_settings_events(self):
        record={'type':'event_msg','payload':{'type':'thread_settings_applied','thread_settings':{'cwd':'/old'}}}
        rewritten,_=cs.rewrite((json.dumps(record)+'\n').encode(),'/old','/new')
        self.assertEqual(json.loads(rewritten)['payload']['thread_settings']['cwd'],'/new')

    def test_project_root_precedence(self):
        repo=self.root/'repo';repo.mkdir();(repo/'.git').write_text('gitdir: elsewhere')
        self.assertEqual(cs.project_root(str(repo/'sub')),(str(repo),'git-root'))
        registered=str(repo/'sub')
        self.assertEqual(cs.project_root(registered,[registered]),(registered,'codex-project-root'))
        self.assertEqual(cs.project_root('Z:\\missing\\repo'),('Z:\\missing\\repo','recorded-cwd'))

    def test_portable_path_boundaries(self):
        self.assertEqual(cs.relative('C:\\work\\repo\\sub','C:\\work\\repo'),('sub',))
        self.assertIsNone(cs.relative('/repo2','/repo'))
        self.assertIsNone(cs.relative('/elsewhere','/repo'))

if __name__=='__main__':unittest.main()
