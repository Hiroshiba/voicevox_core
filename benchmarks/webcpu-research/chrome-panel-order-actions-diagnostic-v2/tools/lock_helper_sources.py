#!/usr/bin/env python3
"""Read/hash existing helper files, copy donor SOURCE as inert .txt; no imports."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
WORKSPACE=ROOT.parents[1]
DONOR=WORKSPACE/'scheduler-actual-operator'
DONOR_MANIFEST_SHA='611acddc3a5e1f502132649b88de68fe4b34b994d2d09a4748c6d6d02ffe7b93'
VENV=WORKSPACE/'off-attribution-recovery/private/venv/lib/python3.12/site-packages'
PSUTIL=Path('/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib/python3.12/site-packages')

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1<<20),b''):h.update(block)
    return h.hexdigest()

def record(path): return {'bytes':path.stat().st_size,'sha256':sha(path)}

def tree_records(directory):
    rows=[]
    for path in sorted(directory.rglob('*')):
        if '__pycache__' in path.parts: continue
        if path.is_symlink(): raise ValueError('helper symlink')
        if path.is_file(): rows.append(dict(name=path.relative_to(directory).as_posix(),**record(path)))
    if not rows: raise ValueError('empty helper tree')
    return rows

def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def build():
    raise RuntimeError("HISTORICAL_FOUR_PACKAGE_GENERATOR_DISABLED_FOR_ACTIVATION: activation fifth-package closure is explicitly reviewed")
    donor_manifest=DONOR/'vendor/manifest.json'
    if sha(donor_manifest)!=DONOR_MANIFEST_SHA:raise ValueError('donor manifest changed')
    donor=json.loads(donor_manifest.read_text())
    if len(donor['files'])!=11:raise ValueError('donor source scope changed')
    references={}
    for name,row in sorted(donor['files'].items()):
        path=DONOR/'vendor'/name
        if path.is_symlink() or record(path)!={'bytes':row['bytes'],'sha256':row['sha256']}:
            raise ValueError('donor byte drift')
        references['vendor/'+name]=path
    references['tools/bootstrap.py']=DONOR/'tools/bootstrap.py'
    references['vendor/manifest.json']=donor_manifest
    target=ROOT/'launch/vendor-reference'
    copied=[]
    for name,path in sorted(references.items()):
        out=target/(name+'.txt');out.parent.mkdir(parents=True,exist_ok=True)
        raw=path.read_bytes();out.write_bytes(raw)
        copied.append(dict(name=name,packaged_reference=out.relative_to(ROOT).as_posix(),**record(path)))
    groups={
        'playwright':(VENV/'playwright',VENV/'playwright-1.63.0.dist-info','1.63.0'),
        'pyee':(VENV/'pyee',VENV/'pyee-13.0.1.dist-info','13.0.1'),
        'greenlet':(VENV/'greenlet',VENV/'greenlet-3.5.6.dist-info','3.5.6'),
        'psutil':(PSUTIL/'psutil',PSUTIL/'psutil-7.2.2.dist-info','7.2.2'),
    }
    runtime={}
    for name,(package,metadata,version) in groups.items():
        package_rows=tree_records(package)
        # INSTALLER/RECORD/direct_url metadata is installer-specific. Its bytes
        # are disclosed separately; only imported package tree is a runtime pin.
        metadata_rows=tree_records(metadata)
        if '\nVersion: '+version+'\n' not in (metadata/'METADATA').read_text():
            raise ValueError('helper version metadata changed')
        runtime[name]=dict(version=version,package_files=package_rows,
                           package_tree_sha256=canonical(package_rows),
                           provenance_metadata=metadata_rows,
                           bytecode_policy='no __pycache__ or .pyc allowed in future installed runtime tree')
    result=dict(schema='panel-bootstrap-helper-source-lock-v1',
                status='HASHED_EXISTING_BYTES_REQUIRES_INDEPENDENT_SOURCE_REVIEW',
                donor_manifest_sha256=DONOR_MANIFEST_SHA,donor_source_files=copied,
                runtime_helper_packages=runtime,
                source_reference_policy='All donor Python stored as inert .py.txt; never import or execute broad donor entrypoints',
                scope='Complete original operator bootstrap helper source and existing runtime helper package bytes; no Chrome bytes, OS tree, compiler execution or fresh installer success claim',
                no_import_install_execution=True)
    path=ROOT/'launch/bootstrap-helper-lock.json'
    path.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return dict(source_files=len(copied),runtime_helper_files=sum(len(x['package_files']) for x in runtime.values()),
                runtime_helper_bytes=sum(row['bytes'] for x in runtime.values() for row in x['package_files']),
                lock_sha256=sha(path),execution_count=0)

if __name__=='__main__':
    def deny(event,_):
        if event.startswith(('subprocess.','socket.','ctypes.dlopen','ctypes.dlsym','os.exec','os.spawn','os.posix_spawn','os.fork','os.system')):
            raise RuntimeError('helper source-lock invocation denied '+event)
    sys.addaudithook(deny);sys.dont_write_bytecode=True
    result=build()
    (ROOT/'evidence/bootstrap-helper-source-lock-audit.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    print(json.dumps(result,sort_keys=True))
