import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import unittest

import compact_backup as compact
import codex_sessions as cs
import test_sessions


class CompactTest(unittest.TestCase):
    setUp=test_sessions.SessionsTest.setUp
    add_session=test_sessions.SessionsTest.add_session

    def populate(self):
        path=self.src/'sessions'/f'rollout-{self.sid}.jsonl'
        image='data:image/png;base64,'+base64.b64encode(os.urandom(4096)).decode()
        # Embedded JSON strings, literal placeholder-like text and noncanonical data.
        record={'type':'response_item','payload':{'content':[{'type':'input_image','image_url':image}]*20,
                 'text':'data:image/png;sha256,literal data:image/png;base64,Zh==',
                 'nested':json.dumps({'url':image}),'unicode':'αβγ'}}
        with path.open('ab') as f:f.write((json.dumps(record,ensure_ascii=False)+'\r\n').encode())

    def export(self,compact_mode=True):
        self.populate()
        return cs.export(self.src,self.project,self.backup,compact=compact_mode)

    def metadata(self):return json.loads((self.backup/'compact.json').read_text())
    def save(self,info):(self.backup/'compact.json').write_text(json.dumps(info))

    def test_exact_reconstruction_and_image_deduplication(self):
        self.export(False);packed=self.root/'packed'
        original={p.relative_to(self.backup):p.read_bytes() for p in self.backup.rglob('*') if p.is_file()}
        result=compact.pack(self.backup,packed)
        self.assertEqual(result['unique_images'],1)
        self.assertEqual(result['image_occurrences'],21)
        self.assertLess(result['stored_bytes'],result['original_bytes'])
        with compact.opened(packed) as expanded:
            self.assertEqual(original,{p.relative_to(expanded):p.read_bytes() for p in expanded.rglob('*') if p.is_file()})
            temp=expanded
        self.assertFalse(temp.exists())

    def test_verify_restore_and_source_untouched(self):
        self.export();before={p:p.read_bytes() for p in self.backup.iterdir()}
        self.assertEqual(set(p.name for p in self.backup.iterdir()),{'compact.json','payload.tar.gz'})
        report=cs.verify(self.backup);self.assertTrue(report['valid'],report)
        result=cs.restore(self.dst,self.backup,str(self.root/'moved'))
        self.assertEqual(result['restored'],1)
        self.assertEqual(before,{p:p.read_bytes() for p in self.backup.iterdir()})
        with self.assertRaisesRegex(ValueError,'conflict'):cs.restore(self.dst,self.backup)

    def test_bulk_compact(self):
        cs.export_all(self.src,self.backup,compact=True)
        report=cs.verify(self.backup);self.assertTrue(report['valid'],report)
        doc=json.loads((self.backup/'index.json').read_text())
        for project in doc['projects']:
            self.assertEqual(project['storage'],'compact')
            self.assertTrue((self.backup/project['directory']/'compact.json').exists())

    def test_corrupted_archive(self):
        self.export();p=self.backup/'payload.tar.gz';p.write_bytes(p.read_bytes()[:-10])
        self.assertFalse(cs.verify(self.backup)['valid'])
        with self.assertRaisesRegex(ValueError,'checksum'):cs.restore(self.dst,self.backup)
        self.assertFalse((self.dst/'sessions').exists())

    def test_traversal_and_recipe_corruption(self):
        self.export();info=self.metadata();info['files'][0]['path']='../outside';self.save(info)
        self.assertFalse(cs.verify(self.backup)['valid']);self.assertFalse((self.root/'outside').exists())
        info['files'][0]['path']='manifest.json';info['files'][0]['recipe']=[{'literal':-1}];self.save(info)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def rewrite_archive(self,mutation):
        path=self.backup/'payload.tar.gz'
        with tarfile.open(path,'r:gz') as tar:
            members=[(m.name,tar.extractfile(m).read()) for m in tar if m.isfile()]
        with tarfile.open(path,'w:gz') as tar:mutation(tar,members)
        info=self.metadata();info['payload_sha256']=compact.digest_file(path);self.save(info)

    def test_archive_symlink_rejected(self):
        self.export()
        def malicious(tar,members):
            item=tarfile.TarInfo(members[0][0]);item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd';tar.addfile(item)
        self.rewrite_archive(malicious)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_blob_tampering_and_missing_objects(self):
        self.export()
        def corrupt(tar,members):
            for name,data in members:
                if name.startswith('blobs/'):data=b'X'*len(data)
                item=tarfile.TarInfo(name);item.size=len(data);tar.addfile(item,io.BytesIO(data))
        self.rewrite_archive(corrupt)
        self.assertFalse(cs.verify(self.backup)['valid'])
        self.rewrite_archive(lambda tar,members:None)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_duplicate_archive_members_rejected(self):
        self.export()
        def duplicate(tar,members):
            for name,data in [members[0],members[0]]:
                item=tarfile.TarInfo(name);item.size=len(data);tar.addfile(item,io.BytesIO(data))
        self.rewrite_archive(duplicate)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_reconstructed_hash_failure(self):
        self.export();info=self.metadata();info['files'][0]['sha256']='0'*64;self.save(info)
        self.assertFalse(cs.verify(self.backup)['valid'])

    def test_cli_compact_export_and_verify(self):
        import subprocess,sys
        run=subprocess.run([sys.executable,'codex_sessions.py','--codex-home',str(self.src),'export',self.project,str(self.backup),'--compact'],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout)['storage'],'compact')
        run=subprocess.run([sys.executable,'codex_sessions.py','verify',str(self.backup)],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout)['storage'],'compact')

if __name__=='__main__':unittest.main()
