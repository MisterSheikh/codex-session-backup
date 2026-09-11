import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import codex_sessions as cs
import test_sessions


class VerifyAssetsTest(unittest.TestCase):
    setUp = test_sessions.SessionsTest.setUp
    add_session = test_sessions.SessionsTest.add_session
    export = test_sessions.SessionsTest.export

    def manifest(self):
        return json.loads((self.backup/'manifest.json').read_text())

    def write_manifest(self, doc):
        (self.backup/'manifest.json').write_text(cs.dumps(doc))

    def add_asset(self):
        asset=self.src/'attachments/upload/photo.png';asset.parent.mkdir(parents=True)
        asset.write_bytes(b'fixture image bytes')
        rollout=self.src/'sessions'/f'rollout-{self.sid}.jsonl'
        record={'type':'response_item','payload':{'type':'message','content':[
            {'type':'local_image','path':str(asset)},
            {'type':'input_image','image_url':'data:image/png;base64,ZmFrZQ=='},
            {'type':'input_text','text':'Attachment: '+str(asset)}]}}
        with rollout.open('ab') as f:f.write((json.dumps(record)+'\n').encode())
        return asset

    def test_verify_offline_and_no_changes(self):
        self.export()
        before={p:p.read_bytes() for p in self.backup.rglob('*') if p.is_file()}
        report=cs.verify(self.backup)
        self.assertTrue(report['valid'],report);self.assertTrue(report['complete'])
        self.assertFalse(report['resume_tested'])
        run=subprocess.run([sys.executable,'codex_sessions.py','--codex-home',str(self.root/'absent'),'verify',str(self.backup)],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertFalse((self.root/'absent').exists())
        self.assertEqual(before,{p:p.read_bytes() for p in self.backup.rglob('*') if p.is_file()})

    def test_assets_opt_in_and_restore_without_source(self):
        asset=self.add_asset()
        cs.export(self.src,self.project,self.backup,include_attachments=True)
        doc=self.manifest();entry=doc['sessions'][0]['assets'][0]
        self.assertEqual(entry['status'],'bundled')
        self.assertEqual(doc['sessions'][0]['embedded_media_records'],1)
        self.assertTrue(cs.verify(self.backup)['complete'])
        asset.unlink()
        result=cs.restore(self.dst,self.backup,str(self.root/'moved'))
        self.assertEqual(result['restored_asset_files'],1)
        restored=next((self.dst/'attachments').rglob('*.png'))
        self.assertEqual(restored.read_bytes(),b'fixture image bytes')
        data=next((self.dst/'sessions').rglob('*.jsonl')).read_text()
        self.assertIn(str(restored),data);self.assertNotIn(str(asset),data)

    def test_omitted_assets_warn(self):
        self.add_asset();self.export()
        report=cs.verify(self.backup)
        self.assertTrue(report['valid']);self.assertFalse(report['complete'])
        self.assertEqual(self.manifest()['sessions'][0]['assets'][0]['status'],'unresolved')

    def test_damaged_asset_prevents_restore(self):
        self.add_asset();cs.export(self.src,self.project,self.backup,include_attachments=True)
        entry=self.manifest()['sessions'][0]['assets'][0]
        (self.backup/entry['file']).write_bytes(b'broken')
        self.assertFalse(cs.verify(self.backup)['valid'])
        with self.assertRaisesRegex(ValueError,'Checksum'):cs.restore(self.dst,self.backup)
        self.assertFalse((self.dst/'sessions').exists())

    def test_symlink_to_auth_not_bundled(self):
        asset=self.add_asset();asset.unlink();asset.symlink_to(self.src/'auth.json')
        cs.export(self.src,self.project,self.backup,include_attachments=True)
        self.assertEqual(self.manifest()['sessions'][0]['assets'][0]['status'],'unresolved')
        self.assertFalse((self.backup/'assets').exists())

    def test_verify_rejects_cross_session_row_and_offset(self):
        self.export();doc=self.manifest()
        doc['sessions'][0]['history']['thread_turns'][0]['rollout_byte_offset']=1
        self.write_manifest(doc)
        self.assertFalse(cs.verify(self.backup)['valid'])
        doc['sessions'][0]['history']['thread_turns'][0]['rollout_byte_offset']=0
        doc['sessions'][0]['history']['thread_turns'][0]['thread_id']='another'
        self.write_manifest(doc)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_verify_version_one(self):
        self.export();doc=self.manifest();doc['version']=1
        doc['sessions'][0].pop('assets');self.write_manifest(doc)
        report=cs.verify(self.backup)
        self.assertTrue(report['valid']);self.assertFalse(report['complete'])
        cs.restore(self.dst,self.backup)

    def test_verify_collection_and_count_tampering(self):
        cs.export_all(self.src,self.backup)
        report=cs.verify(self.backup);self.assertTrue(report['valid'],report)
        path=self.backup/'index.json';doc=json.loads(path.read_text());doc['exported']+=1;path.write_text(cs.dumps(doc))
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_missing_malformed_and_wrong_id(self):
        self.export();doc=self.manifest();p=self.backup/doc['sessions'][0]['file']
        data=p.read_bytes();p.unlink();self.assertFalse(cs.verify(self.backup)['valid'])
        p.write_bytes(b'[]\n');doc['sessions'][0]['sha256']=hashlib.sha256(p.read_bytes()).hexdigest();self.write_manifest(doc)
        self.assertFalse(cs.verify(self.backup)['valid'])
        p.write_bytes(data);doc['sessions'][0]['sha256']=hashlib.sha256(data).hexdigest()
        doc['sessions'][0]['thread']['id']='other';self.write_manifest(doc)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_asset_conflict_preserves_existing_file(self):
        self.add_asset();cs.export(self.src,self.project,self.backup,include_attachments=True)
        entry=self.manifest()['sessions'][0]['assets'][0]
        dest=self.dst/'attachments/restored'/self.sid/Path(entry['file']).name
        dest.parent.mkdir(parents=True);dest.write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError,'conflict'):cs.restore(self.dst,self.backup)
        self.assertEqual(dest.read_bytes(),b'existing')

    def test_remote_media_not_downloaded(self):
        from session_assets import collect
        entries=collect({'https://example.invalid/image.png'},self.src,self.root,self.sid,True)
        self.assertEqual(entries[0]['status'],'unresolved')
        self.assertFalse((self.root/'assets').exists())

    def test_asset_path_traversal_and_missing_inventory(self):
        self.add_asset();cs.export(self.src,self.project,self.backup,include_attachments=True)
        doc=self.manifest();doc['sessions'][0]['assets'][0]['file']='../auth.json';self.write_manifest(doc)
        self.assertFalse(cs.verify(self.backup)['valid'])
        doc['sessions'][0].pop('assets');self.write_manifest(doc)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_collection_traversal_and_unassigned_damage(self):
        raw=self.src/'sessions/old.jsonl';raw.write_text('{"id":"old"}\n')
        cs.export_all(self.src,self.backup)
        report=cs.verify(self.backup);self.assertTrue(report['valid']);self.assertFalse(report['complete'])
        index=self.backup/'index.json';doc=json.loads(index.read_text())
        (self.backup/doc['unassigned'][0]['file']).write_text('broken')
        self.assertFalse(cs.verify(self.backup)['valid'])
        doc['projects'][0]['directory']='../source';index.write_text(cs.dumps(doc))
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_attachment_failure_rolls_back_database(self):
        from unittest.mock import patch
        import sqlite3
        self.add_asset();cs.export(self.src,self.project,self.backup,include_attachments=True)
        original=os.open
        def fail(path,flags,*args,**kwargs):
            if '/sessions/' in str(path):raise OSError('simulated rollout write failure')
            return original(path,flags,*args,**kwargs)
        with patch.object(cs.os,'open',side_effect=fail):
            with self.assertRaisesRegex(OSError,'simulated'):cs.restore(self.dst,self.backup)
        self.assertEqual(list((self.dst/'attachments').rglob('*.png')),[])
        with sqlite3.connect(self.dst/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM threads').fetchone()[0],0)

    def test_attachment_path_prefix_does_not_change_other_file(self):
        from session_assets import replace_references
        self.assertEqual(replace_references('/old/a.png.backup',{'/old/a.png':'/new/a.png'}),'/old/a.png.backup')

if __name__=='__main__':unittest.main()
