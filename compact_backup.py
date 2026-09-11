"""Lossless project container: byte recipes, deduplicated images and gzip tar."""
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tempfile

IMAGE = re.compile(rb'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/]*={0,2}')
CHUNK = 1024 * 1024


def digest_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(CHUNK),b''):digest.update(data)
    return digest.hexdigest()


def safe_path(root,name):
    from backup_verify import contained
    return contained(root,name)


def pack(source,destination):
    if destination.exists():raise ValueError(f'Destination already exists: {destination}')
    if not (source/'manifest.json').is_file():raise ValueError('Compact storage supports individual project backups')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        root=Path(tmp);objects=root/'objects';objects.mkdir();result=root/'backup';result.mkdir(mode=0o700)
        info={'format':'codex-compact-project','version':1,'compression':'gzip','files':[],'blobs':{}}
        original_bytes=0;occurrences=0
        for number,path in enumerate(sorted(p for p in source.rglob('*') if p.is_file())):
            if path.is_symlink():raise ValueError('Symlinks are not supported in compact backups')
            body=f'files/{number:06d}';target=objects/body;target.parent.mkdir(exist_ok=True)
            recipe=[];literal=0;digest=hashlib.sha256();size=0
            with path.open('rb') as f,target.open('wb') as out:
                # JSONL lines bound the scanner; non-JSON binaries use bounded chunks.
                stream=f if path.suffix in ('.json','.jsonl') else iter(lambda:f.read(CHUNK),b'')
                for line in stream:
                    digest.update(line);size+=len(line);start=0
                    matches=IMAGE.finditer(line) if path.suffix in ('.json','.jsonl') else ()
                    for match in matches:
                        prefix,encoded=match.group().split(b';base64,',1)
                        try:
                            binary=base64.b64decode(encoded,validate=True)
                            if not binary or base64.b64encode(binary)!=encoded:continue
                        except ValueError:continue
                        key=hashlib.sha256(binary).hexdigest();blob='blobs/'+key
                        if key not in info['blobs']:
                            (objects/'blobs').mkdir(exist_ok=True)
                            (objects/blob).write_bytes(binary)
                            info['blobs'][key]={'file':blob,'size':len(binary)}
                        out.write(line[start:match.start()]);literal+=match.start()-start
                        if literal:recipe.append({'literal':literal});literal=0
                        recipe.append({'image':key,'prefix':prefix.decode('ascii')+';base64,'})
                        occurrences+=1;start=match.end()
                    out.write(line[start:]);literal+=len(line)-start
                if literal:recipe.append({'literal':literal})
            info['files'].append({'path':path.relative_to(source).as_posix(),'body':body,'size':size,'sha256':digest.hexdigest(),'recipe':recipe})
            original_bytes+=size
        archive=result/'payload.tar.gz'
        with tarfile.open(archive,'w:gz',compresslevel=6,format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(objects.rglob('*')):
                if path.is_file():tar.add(path,arcname=path.relative_to(objects).as_posix(),recursive=False)
        info['payload_sha256']=digest_file(archive)
        info['original_bytes']=original_bytes;info['image_occurrences']=occurrences
        (result/'compact.json').write_text(json.dumps(info,indent=2)+'\n')
        for path in result.iterdir():path.chmod(0o600)
        result.rename(destination)
    return {'storage':'compact','original_bytes':original_bytes,'stored_bytes':sum(p.stat().st_size for p in destination.iterdir()),'unique_images':len(info['blobs']),'image_occurrences':occurrences}


@contextmanager
def opened(backup):
    """Yield a normal directory; compact contents are verified before use."""
    backup=Path(backup)
    if not (backup/'compact.json').exists():
        yield backup;return
    try:
        info=json.loads((backup/'compact.json').read_text())
        if info.get('format')!='codex-compact-project' or info.get('version')!=1 or info.get('compression')!='gzip':raise ValueError('Unsupported compact backup format')
        if (backup/'manifest.json').exists() or (backup/'index.json').exists():raise ValueError('Ambiguous compact backup')
        archive=backup/'payload.tar.gz'
        if digest_file(archive)!=info['payload_sha256']:raise ValueError('Compact payload checksum mismatch')
        with tempfile.TemporaryDirectory(prefix='codex-unpack-') as tmp:
            root=Path(tmp);objects=root/'objects';objects.mkdir();output=root/'project';output.mkdir()
            expected={};names=set()
            for entry in info['files']:
                path=safe_path(output,entry['path'])
                name=entry['path']
                if name!='manifest.json' and not name.startswith(('rollouts/','segments/','assets/')):raise ValueError('Unexpected reconstructed file')
                if path in names:raise ValueError('Duplicate reconstructed file')
                names.add(path)
                if type(entry['size']) is not int or entry['size']<0:raise ValueError('Invalid file size')
                literal=0;total=0
                for part in entry['recipe']:
                    if set(part)=={'literal'}:
                        length=part['literal']
                        if type(length) is not int or length<0:raise ValueError('Invalid literal length')
                        literal+=length;total+=length
                    elif set(part)=={'image','prefix'}:
                        blob=info['blobs'][part['image']]
                        if not re.fullmatch(r'data:image/[A-Za-z0-9.+-]+;base64,',part['prefix']):raise ValueError('Invalid image prefix')
                        total+=len(part['prefix'])+4*((blob['size']+2)//3)
                    else:raise ValueError('Invalid reconstruction recipe')
                if total!=entry['size']:raise ValueError('Recipe size mismatch')
                if not re.fullmatch(r'files/[0-9]{6}',entry['body']) or entry['body'] in expected:raise ValueError('Invalid/duplicate body object')
                expected[entry['body']]=literal
            if not any(e['path']=='manifest.json' for e in info['files']):raise ValueError('Missing project manifest')
            for key,entry in info['blobs'].items():
                if not re.fullmatch('[0-9a-f]{64}',key) or entry['file']!='blobs/'+key or type(entry['size']) is not int or entry['size']<0:raise ValueError('Invalid blob metadata')
                expected[entry['file']]=entry['size']
            found=set()
            with tarfile.open(archive,'r|gz') as tar:
                for member in tar:
                    if not member.isfile() or member.issparse() or member.name not in expected or member.name in found or member.size!=expected[member.name]:raise ValueError('Unexpected, duplicate, or invalid archive member')
                    found.add(member.name);target=safe_path(objects,member.name);target.parent.mkdir(parents=True,exist_ok=True)
                    stream=tar.extractfile(member)
                    with target.open('wb') as out:
                        remaining=member.size
                        while remaining:
                            chunk=stream.read(min(CHUNK,remaining))
                            if not chunk:raise ValueError('Truncated archive member')
                            out.write(chunk);remaining-=len(chunk)
            if found!=set(expected):raise ValueError('Missing archive objects')
            for key,entry in info['blobs'].items():
                if digest_file(objects/entry['file'])!=key:raise ValueError('Image blob checksum mismatch')
            for entry in info['files']:
                target=safe_path(output,entry['path']);target.parent.mkdir(parents=True,exist_ok=True)
                with (objects/entry['body']).open('rb') as body,target.open('wb') as out:
                    for part in entry['recipe']:
                        if 'literal' in part:
                            remaining=part['literal']
                            while remaining:
                                chunk=body.read(min(CHUNK,remaining))
                                if not chunk:raise ValueError('Truncated literal body')
                                out.write(chunk);remaining-=len(chunk)
                        else:
                            out.write(part['prefix'].encode('ascii'))
                            # A multiple of three keeps base64 chunks independently encodable.
                            with (objects/info['blobs'][part['image']]['file']).open('rb') as image:
                                for chunk in iter(lambda:image.read(3*262144),b''):out.write(base64.b64encode(chunk))
                    if body.read(1):raise ValueError('Unused body bytes')
                if target.stat().st_size!=entry['size'] or digest_file(target)!=entry['sha256']:raise ValueError('Reconstructed file checksum mismatch')
            yield output
    except (tarfile.TarError,KeyError,TypeError,AttributeError,EOFError) as e:
        raise ValueError(f'Invalid compact backup: {e}') from e
