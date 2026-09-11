import json
import sqlite3
import uuid
import unittest
from pathlib import Path
import codex_sessions as cs
import session_chains as chains
import test_sessions


class ChainTest(unittest.TestCase):
    add_session=test_sessions.SessionsTest.add_session

    def setUp(self):
        test_sessions.SessionsTest.setUp(self)
        self.parent=self.src/'sessions'/f'rollout-{self.sid}.jsonl'
        records=[json.loads(line) for line in self.parent.read_bytes().splitlines()]
        for i,r in enumerate(records):r['ordinal']=i
        self.parent.write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in records))
        with sqlite3.connect(self.src/'thread_history_1.sqlite') as db:
            size=self.parent.stat().st_size
            db.execute('UPDATE thread_turns SET rollout_end_byte_offset=? WHERE thread_id=?',(size,self.sid))
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?',(size,self.sid))
        self.branch=str(uuid.uuid4());self.tip=self.add_segment(self.sid,self.branch,self.parent,2)
        with sqlite3.connect(self.src/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(self.tip),self.sid))

    def add_segment(self,session_id,key,parent,cutoff):
        pm=cs.file_meta(parent);parent_key=chains.thread_id(parent,pm)
        end=chains.boundaries(parent.read_bytes())[cutoff]
        m={'id':session_id,'cwd':self.project+'/child','timestamp':'2026-09-02T12:00:00Z','history_base':{'thread_id':parent_key,'end_ordinal_exclusive':cutoff,'end_byte_offset':end}}
        records=[{'type':'session_meta','ordinal':cutoff,'payload':m},{'type':'turn_context','ordinal':cutoff+1,'payload':{'cwd':self.project+'/child'}}]
        path=self.src/'sessions'/f'rollout-{session_id}_{key}.jsonl'
        path.write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in records))
        with sqlite3.connect(self.src/'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',(key,'branch-turn',0,path.stat().st_size))
            db.execute('INSERT INTO thread_history_projection_state VALUES (?,?)',(key,path.stat().st_size))
        return path

    def export(self,paths=None):
        return cs.export(self.src,self.project,self.backup,selected_paths=paths)

    def test_export_preserves_both_files_and_histories(self):
        self.export();doc=json.loads((self.backup/'manifest.json').read_text());session=doc['sessions'][0]
        self.assertEqual(session['active_thread_id'],self.branch)
        self.assertEqual(len(session['segments']),2)
        self.assertEqual((self.backup/session['segments'][0]['file']).read_bytes(),self.parent.read_bytes())
        self.assertEqual(session['segments'][1]['history']['thread_turns'][0]['thread_id'],self.branch)
        self.assertTrue(cs.verify(self.backup)['valid'])

    def test_restore_remaps_base_and_history_offsets(self):
        self.export();out=cs.restore(self.dst,self.backup,str(self.root/'much-longer-project-path'))
        self.assertEqual(out['restored_segments'],2)
        paths=list(cs.rollout_files(self.dst));catalog=chains.catalog(paths)
        tip=catalog[self.branch][0];parent=catalog[self.sid][0]
        base=cs.file_meta(tip)['history_base']
        self.assertEqual(base['end_byte_offset'],chains.boundaries(parent.read_bytes())[2])
        self.assertEqual(cs.file_meta(tip)['cwd'],str(self.root/'much-longer-project-path/child'))
        with sqlite3.connect(self.dst/'thread_history_1.sqlite') as db:
            for key in (self.sid,self.branch):
                offset=db.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?',(key,)).fetchone()[0]
                self.assertEqual(offset,catalog[key][0].stat().st_size)
        with self.assertRaisesRegex(ValueError,'conflict'):cs.restore(self.dst,self.backup)

    def test_missing_base_fails_export(self):
        self.parent.unlink()
        with self.assertRaisesRegex(ValueError,'history_base'):self.export([self.tip])
        self.assertFalse(self.backup.exists())

    def test_verify_missing_segment_and_bad_cutoff(self):
        self.export();p=self.backup/'manifest.json';doc=json.loads(p.read_text());session=doc['sessions'][0]
        original=session['segments'][:];session['segments']=session['segments'][1:];p.write_text(cs.dumps(doc))
        self.assertFalse(cs.verify(self.backup)['valid'])
        session['segments']=original;p.write_text(cs.dumps(doc))
        parent_path=self.backup/session['segments'][0]['file'];parent_path.unlink()
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_cycles_and_invalid_cutoff(self):
        m=cs.file_meta(self.tip);m['history_base']['thread_id']=self.branch
        with self.tip.open('w') as f:f.write(json.dumps({'type':'session_meta','payload':m})+'\n')
        with self.assertRaisesRegex(ValueError,'Cyclic'):self.export([self.tip])

    def test_shared_ancestors_restored_once(self):
        child=str(uuid.uuid4());tip2=self.add_segment(child,child,self.tip,4)
        with sqlite3.connect(self.src/'state_5.sqlite') as db:
            db.execute('INSERT INTO threads SELECT ?,?,cwd,created_at,updated_at,title,archived,history_mode FROM threads WHERE id=?',(child,str(tip2),self.sid))
        self.export([self.tip,tip2]);report=cs.verify(self.backup);self.assertTrue(report['valid'],report)
        result=cs.restore(self.dst,self.backup,str(self.root/'moved'))
        self.assertEqual(result['restored'],2);self.assertEqual(result['restored_segments'],3)
        self.assertEqual(len(list(cs.rollout_files(self.dst))),3)

    def test_foreign_project_dependency_fails_explicitly(self):
        records=[json.loads(line) for line in self.parent.read_bytes().splitlines()]
        records[0]['payload']['cwd']='/some-other-project'
        self.parent.write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in records))
        with self.assertRaisesRegex(ValueError,'Cross-project'):self.export([self.tip])

    def test_legacy_linked_backup_is_rejected(self):
        self.export();p=self.backup/'manifest.json';doc=json.loads(p.read_text())
        doc['version']=1;doc['sessions'][0].pop('segments');p.write_text(cs.dumps(doc))
        with self.assertRaisesRegex(ValueError,'history segments'):cs.restore(self.dst,self.backup)

    def test_invalid_base_cutoff(self):
        records=[json.loads(line) for line in self.tip.read_bytes().splitlines()]
        records[0]['payload']['history_base']['end_byte_offset']+=1
        self.tip.write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in records))
        with self.assertRaisesRegex(ValueError,'cutoff'):self.export([self.tip])

    def test_bulk_groups_linked_base_with_owning_session(self):
        from unittest.mock import patch
        with patch.object(cs,'project_root',side_effect=lambda cwd,registered:(self.project if cs.relative(cwd,self.project) is not None else cwd,'test-root')):
            result=cs.export_all(self.src,self.backup)
        self.assertTrue(result['complete'],result)
        self.assertEqual(result['unassigned'],[])
        self.assertEqual(result['exported'],2)
        self.assertTrue(cs.verify(self.backup)['valid'])

    def test_native_failure_rolls_back_all_rows(self):
        from unittest.mock import patch
        import os
        self.export();original=os.open
        def fail(path,flags,*args,**kwargs):
            if self.branch in str(path):raise OSError('simulated continuation write failure')
            return original(path,flags,*args,**kwargs)
        with patch.object(os,'open',side_effect=fail):
            with self.assertRaisesRegex(OSError,'simulated'):cs.restore(self.dst,self.backup)
        self.assertEqual(list(cs.rollout_files(self.dst)),[])
        with sqlite3.connect(self.dst/'state_5.sqlite') as db:self.assertEqual(db.execute('SELECT count(*) FROM threads').fetchone()[0],0)
        with sqlite3.connect(self.dst/'thread_history_1.sqlite') as db:self.assertEqual(db.execute('SELECT count(*) FROM thread_turns').fetchone()[0],0)

if __name__=='__main__':unittest.main()
