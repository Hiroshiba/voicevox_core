"""Bounded official acquisition and extraction; no installer or subprocess.

This source-review release cannot enter any I/O path without execution admission.
Pure path/manifest predicates are separately usable by source-only tests.
"""
if __name__ == '__main__':
    raise SystemExit('SOURCE_ONLY_EXECUTION_BLOCK: launch.bootstrap_fetch')

from launch.source_only import require_execution_admission


def validate_download(row):
    """Check an already supplied download row without any I/O."""
    from urllib.parse import urlsplit
    allowed = {'github.com', 'codeload.github.com', 'files.pythonhosted.org', 'storage.googleapis.com'}
    url = urlsplit(row['url'])
    if url.scheme != 'https' or url.hostname not in allowed or url.username or url.password or url.port not in (None, 443):
        raise ValueError('bootstrap_download_origin')
    if type(row['bytes']) is not int or not 0 < row['bytes'] <= 512 * 1024**2:
        raise ValueError('bootstrap_download_size')
    digest = row['sha256']
    if digest is None:
        if row['id'] not in ('sdk-node', 'sdk-tools') or row.get('identity_policy') != 'official_once_acquired_archive_hash_then_prepinned_tools':
            raise ValueError('bootstrap_unpinned_distribution')
    elif not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('bootstrap_download_hash')
    if row['format'] not in ('zip', 'tar.gz', 'tar.xz') or row['strip_components'] not in (0, 1):
        raise ValueError('bootstrap_download_format')
    return True


def safe_name(name, strip_components=0):
    from pathlib import PurePosixPath
    p = PurePosixPath(name)
    if not name or '\\' in name or '\x00' in name or p.is_absolute() or '..' in p.parts or p.as_posix() != name.rstrip('/'):
        raise ValueError('bootstrap_member_path')
    parts = p.parts[strip_components:]
    return '/'.join(parts)


def initial_layout(provision_root):
    """Pure fresh-parent layout for the pre-supervisor bootstrap."""
    from pathlib import PurePosixPath
    root=PurePosixPath(provision_root)
    if not root.is_absolute() or '..' in root.parts:raise ValueError('bootstrap_initial_layout')
    return {'parent':str(root.parent),'root':str(root),'mkdir_parent_first':True,
            'new_subdirectories':['archives','deps/python']}


def checked_link(name, target):
    """Resolve a relative link lexically; its installed destination is checked too."""
    import posixpath
    if not target or target.startswith('/') or '\\' in target or '\x00' in target:
        raise ValueError('bootstrap_link_text')
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    safe_name(resolved)
    return resolved


def validate_symlink_roster(rows):
    """Pure resolution of exact relative links, including chains/dir prefixes."""
    links={r['name']:r for r in rows}
    if len(links)!=len(rows):raise ValueError('bootstrap_duplicate_link_pin')
    result={}
    for name,spec in links.items():
        safe_name(name)
        if spec.get('target_kind','file') not in ('file','directory'):raise ValueError('bootstrap_link_kind')
        value=checked_link(name,spec['link_text']);seen={name}
        for step in range(len(links)+1):
            parts=value.split('/');prefix=next(('/'.join(parts[:i]) for i in range(1,len(parts)+1) if '/'.join(parts[:i]) in links),None)
            if prefix is None:break
            if value in seen:raise ValueError('bootstrap_link_cycle')
            seen.add(value);suffix=value[len(prefix):].lstrip('/')
            value=checked_link(prefix,links[prefix]['link_text'])+('/'+suffix if suffix else '')
            safe_name(value)
        else:raise ValueError('bootstrap_link_chain_budget')
        if value!=spec['resolved_suffix']:raise ValueError('bootstrap_link_resolution_pin')
        result[name]=value
    return result


def _clock(deadline):
    require_execution_admission('bootstrap_fetch._clock')
    import time
    if time.monotonic() >= deadline:
        raise TimeoutError('bootstrap_absolute_deadline')


def file_record(path, deadline):
    require_execution_admission('bootstrap_fetch.file_record')
    import hashlib
    from pathlib import Path
    p = Path(path); h = hashlib.sha256(); size = 0
    with p.open('rb') as stream:
        while True:
            _clock(deadline); block = stream.read(1024**2)
            if not block: break
            h.update(block); size += len(block)
    return {'bytes': size, 'sha256': h.hexdigest()}


def acquire(row, destination, deadline):
    """One GET with a hard wall alarm; no ambient proxy, retry or alternate URL."""
    require_execution_admission('bootstrap_fetch.acquire')
    import hashlib
    import signal
    import time
    import urllib.request
    from pathlib import Path
    from urllib.parse import urlsplit
    validate_download(row); _clock(deadline)
    target = Path(destination)
    if target.exists() or target.is_symlink(): raise ValueError('bootstrap_download_not_fresh')
    target.parent.mkdir(parents=True, exist_ok=True)
    redirects = []
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            u = urlsplit(newurl); origin = urlsplit(row['url'])
            permitted = (origin.hostname == 'github.com' and u.hostname in ('codeload.github.com', 'release-assets.githubusercontent.com'))
            if not permitted or u.scheme != 'https' or u.username or u.password or u.port not in (None,443) or redirects:
                raise ValueError('bootstrap_redirect_not_admitted')
            # GitHub's signed release URL can change; the original archive has a
            # full pre-existing digest. SDK hash-acquired URLs cannot redirect.
            if row['sha256'] is None: raise ValueError('bootstrap_unpinned_redirect')
            redirects.append({'status':code,'host':u.hostname,'path':u.path})
            return urllib.request.Request(newurl, headers=dict(req.header_items()), method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    def expired(signum, frame): raise TimeoutError('bootstrap_absolute_network_deadline')
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic(); remaining = deadline - started
    if old_timer[0]: remaining = min(remaining, old_timer[0])
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(.001,remaining))
    checksum = hashlib.sha256(); total = 0;failed=None;complete=False
    journal=target.parent/'bootstrap-transfers.private.jsonl'
    import json
    with journal.open('ab') as stream:stream.write((json.dumps({'id':row['id'],'attempted':True,'acknowledged':False})+'\n').encode())
    try:
        request = urllib.request.Request(row['url'], headers={'Accept-Encoding':'identity','User-Agent':'panel-pinned-bootstrap/1'}, method='GET')
        with opener.open(request,timeout=min(15,max(.001,remaining))) as response, target.open('xb') as out:
            if response.status != 200: raise ValueError('bootstrap_http_status')
            lengths = response.headers.get_all('Content-Length', [])
            if len(lengths)>1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) != row['bytes'])):
                raise ValueError('bootstrap_content_length')
            encodings=response.headers.get_all('Content-Encoding', [])
            if len(encodings)>1 or (encodings and encodings[0].lower() not in ('','identity')):
                raise ValueError('bootstrap_transfer_transform')
            while True:
                _clock(deadline); block = response.read(1024**2)
                if not block: break
                total += len(block)
                if total > row['bytes']: raise ValueError('bootstrap_compressed_overrun')
                out.write(block); checksum.update(block)
        actual = checksum.hexdigest()
        if total != row['bytes'] or (row['sha256'] is not None and actual != row['sha256']):
            raise ValueError('bootstrap_archive_identity')
        complete=True
        return {'id':row['id'],'url':row['url'],'bytes':total,'sha256':actual,'expected_sha256':row['sha256'],
                'identity_policy':row.get('identity_policy','preexisting_archive_sha256'),'get_requests':1,'retries':0,
                'redirects':redirects,'wall_seconds':time.monotonic()-started,'operator_calls':0,'browser_launches':0}
    except BaseException as error:
        failed=type(error).__name__;raise
    finally:
        record={'id':row['id'],'attempted':True,'acknowledged':complete,'first_failure':failed,'received_bytes':total,'received_sha256':checksum.hexdigest(),'operator_calls':0,'browser_launches':0}
        if journal.stat().st_size>2*1024**2:raise ValueError('bootstrap_transfer_journal_budget')
        with journal.open('ab') as stream:stream.write((json.dumps(record,sort_keys=True)+'\n').encode())
        signal.setitimer(signal.ITIMER_REAL, 0); signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]: signal.setitimer(signal.ITIMER_REAL,max(.001,old_timer[0]-(time.monotonic()-started)),old_timer[1])


def extract(row, archive_path, destination, deadline):
    """Extract regular files first, then exact allowlisted links. No extractall."""
    require_execution_admission('bootstrap_fetch.extract')
    import os
    import stat
    import tarfile
    import zipfile
    from pathlib import Path
    validate_download(row); _clock(deadline)
    root=Path(destination)
    if root.exists() or root.is_symlink(): raise ValueError('bootstrap_extract_not_fresh')
    root.mkdir(parents=True)
    validate_symlink_roster(row.get('symlinks',[]))
    expected_links={r['name']:r for r in row.get('symlinks',[])}
    if len(expected_links)!=len(row.get('symlinks',[])): raise ValueError('bootstrap_duplicate_link_pin')
    names=set(); links={}; expanded=0; count=0; first_roots=set(); skipped=[]
    def destination_for(raw):
        first_roots.add(raw.split('/',1)[0]); name=safe_name(raw,row['strip_components'])
        if not name:return None,None
        if name in names:raise ValueError('bootstrap_duplicate_archive_path')
        names.add(name);p=root/name
        for ancestor in p.parents:
            if ancestor==root:break
            if ancestor.is_symlink():raise ValueError('bootstrap_symlink_parent')
        return name,p
    def member(raw, size, mode, kind, stream=None, link_text=None):
        nonlocal expanded,count
        _clock(deadline);count+=1
        if count>row['maximum_members']:raise ValueError('bootstrap_member_count')
        name,target=destination_for(raw)
        if target is None:return
        if mode & 0o7000:raise ValueError('bootstrap_special_permissions')
        if kind=='directory':target.mkdir(parents=True,exist_ok=True);return
        if kind=='symlink':
            spec=expected_links.get(name)
            if spec is None or spec['link_text']!=link_text:
                raise ValueError('bootstrap_link_not_pinned')
            checked_link(name,link_text)
            links[name]=link_text;return
        if kind!='file' or not 0<=size<=row['maximum_member_bytes']:raise ValueError('bootstrap_nonregular_or_oversize')
        expanded+=size
        if expanded>row['maximum_expanded_bytes']:raise ValueError('bootstrap_expanded_size')
        # Existing SDK bytecode is interpreter-specific. No bytecode is installed
        # or imported; omissions are recorded and cannot change source bytes.
        discard = '__pycache__' in name.split('/') or name.endswith('.pyc')
        if discard and not row.get('omit_bytecode',False):raise ValueError('bootstrap_unexpected_bytecode')
        target.parent.mkdir(parents=True,exist_ok=True);total=0
        out=None if discard else target.open('xb')
        try:
            while True:
                _clock(deadline);block=stream.read(min(1024**2,size-total+1))
                if not block:break
                total+=len(block)
                if total>size:raise ValueError('bootstrap_expanded_overrun')
                if out:out.write(block)
            if total!=size:raise ValueError('bootstrap_expanded_truncated')
        finally:
            if out:out.close()
        if discard:skipped.append(name)
        else:os.chmod(target,0o755 if mode&0o111 else 0o644)
    if row['format']=='zip':
        with zipfile.ZipFile(archive_path) as z:
            for info in z.infolist():
                if info.flag_bits&1 or info.compress_type not in (0,8):raise ValueError('bootstrap_zip_encoding')
                mode=(info.external_attr>>16)&0xffff;kind=stat.S_IFMT(mode)
                if info.is_dir():member(info.filename,0,mode,'directory')
                elif kind==stat.S_IFLNK:
                    if info.file_size>4096:raise ValueError('bootstrap_link_size')
                    member(info.filename,info.file_size,mode,'symlink',link_text=z.read(info).decode('utf-8'))
                elif kind in (0,stat.S_IFREG):
                    with z.open(info) as stream:member(info.filename,info.file_size,mode,'file',stream)
                else:raise ValueError('bootstrap_zip_special_file')
    else:
        with tarfile.open(archive_path,mode='r:gz' if row['format']=='tar.gz' else 'r:xz') as tar:
            for info in tar:
                if info.isdir():member(info.name,0,info.mode,'directory')
                elif info.issym():member(info.name,0,info.mode,'symlink',link_text=info.linkname)
                elif info.isfile():
                    with tar.extractfile(info) as stream:member(info.name,info.size,info.mode,'file',stream)
                else:raise ValueError('bootstrap_tar_special_or_hardlink')
    if row['strip_components']==1 and len(first_roots)!=1:raise ValueError('bootstrap_tar_common_root')
    if set(links)!=set(expected_links):raise ValueError('bootstrap_symlink_roster')
    for name in links:
        if any(n.startswith(name+'/') for n in names):raise ValueError('bootstrap_archive_link_ancestor_collision')
    for name,text in sorted(links.items()):
        _clock(deadline);target=root/name
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() or target.is_symlink():raise ValueError('bootstrap_link_collision')
        target.symlink_to(text)
    for name,spec in expected_links.items():
        resolved=(root/name).resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or resolved.relative_to(root.resolve()).as_posix()!=spec['resolved_suffix'] or (not resolved.is_dir() if spec.get('target_kind')=='directory' else not resolved.is_file()):
            raise ValueError('bootstrap_installed_link_escape')
    return {'id':row['id'],'members':count,'expanded_bytes':expanded,'symlinks':len(links),'skipped_bytecode':skipped}


def verify_roster(root, rows, deadline, exact=True):
    require_execution_admission('bootstrap_fetch.verify_roster')
    from pathlib import Path
    root=Path(root);expected={r['name']:r for r in rows}
    if len(expected)!=len(rows):raise ValueError('bootstrap_duplicate_file_pin')
    if root.is_symlink():raise ValueError('bootstrap_package_root_link')
    seen=set()
    for path in root.rglob('*'):
        _clock(deadline)
        if path.is_symlink():raise ValueError('bootstrap_package_link')
        if path.is_file():seen.add(path.relative_to(root).as_posix())
    if (exact and seen!=set(expected)) or not set(expected)<=seen:raise ValueError('bootstrap_package_roster')
    for name,row in expected.items():
        safe_name(name);p=root/name
        if file_record(p,deadline)!={'bytes':row['bytes'],'sha256':row['sha256']}:raise ValueError('bootstrap_package_bytes')
    return {'files':len(rows),'verified':True}


def _bootstrap_psutil_work(context, deadline):
    """Initial 120s stdlib-only work plus 30s reserve; no supervision claim."""
    require_execution_admission('bootstrap_fetch._bootstrap_psutil_work')
    import json
    import shutil
    import time
    from pathlib import Path
    started=time.monotonic();work_end=min(deadline-30,started+120)
    layout=initial_layout(context['provision_root'])
    root=Path(layout['root']);source=Path(context['roots']['SOURCE'])
    if not root.is_absolute() or root.exists() or root.is_symlink():raise ValueError('bootstrap_initial_root_not_fresh')
    if any(p.is_symlink() for p in root.parents):raise ValueError('bootstrap_initial_parent_link')
    root.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(root.parent).free<12*1024**3:raise ValueError('bootstrap_initial_disk')
    root.mkdir();(root/'archives').mkdir();(root/'deps/python').mkdir(parents=True)
    inputs=json.loads((source/'launch/bootstrap-inputs.json').read_text())
    helper=json.loads((source/'launch/bootstrap-helper-lock.json').read_text())
    if helper!=context['bootstrap_helper_lock']:raise ValueError('bootstrap_helper_lock_context_drift')
    row=next(r for r in inputs['downloads'] if r['id']=='wheel-psutil')
    receipt={'schema':'panel-initial-psutil-v1','started_monotonic':started,'work_deadline':work_end,
             'cleanup_deadline':min(deadline,started+150),'production_supervised':False,'subprocesses':0,
             'observation_scope':'stdlib byte, wall and disk checks only; no production RSS/tree/CPU claim',
             'complete':False,'attempted':True,'acknowledged':False,'first_failure':None,'browser_launches':0,'operator_calls':0}
    (root/'bootstrap-owner.json').write_text(json.dumps({'schema':'panel-owned-bootstrap-root-v1','source':str(source)},sort_keys=True))
    (root/'bootstrap-psutil-receipt.json').write_text(json.dumps(receipt,sort_keys=True))
    try:
        receipt['download']=acquire(row,root/'archives'/row['filename'],work_end)
        staging=root/'initial-psutil-wheel';receipt['extraction']=extract(row,root/'archives'/row['filename'],staging,work_end)
        package=staging/'psutil';verify_roster(package,helper['runtime_helper_packages']['psutil']['package_files'],work_end)
        package.rename(root/'deps/python/psutil');_clock(work_end)
        if shutil.disk_usage(root).free<4*1024**3:raise ValueError('bootstrap_disk_reserve')
        receipt['complete']=True;receipt['acknowledged']=True;receipt['package_root']=str(root/'deps/python/psutil')
        receipt['runtime_helper_package_roots']={name:str(root/'deps/python'/('typing_extensions.py' if name=='typing_extensions' else name)) for name in helper['runtime_helper_packages']}
        return receipt
    except BaseException as error:
        receipt['first_failure']=type(error).__name__;raise
    finally:
        receipt['wall_seconds']=time.monotonic()-started
        (root/'bootstrap-psutil-receipt.json').write_text(json.dumps(receipt,sort_keys=True))


def bootstrap_psutil(context, deadline):
    """Hard 150-second total alarm around download, extraction, hash and receipt."""
    require_execution_admission('bootstrap_fetch.bootstrap_psutil')
    import signal
    import time
    started=time.monotonic();end=min(deadline,started+150)
    if end-started<=30:raise TimeoutError('bootstrap_initial_cleanup_reserve')
    old_handler=signal.getsignal(signal.SIGALRM);old_timer=signal.getitimer(signal.ITIMER_REAL)
    def expired(signum,frame):raise TimeoutError('bootstrap_initial_hard_150_seconds')
    signal.signal(signal.SIGALRM,expired)
    limit=end-started
    if old_timer[0]:limit=min(limit,old_timer[0])
    signal.setitimer(signal.ITIMER_REAL,max(.001,limit))
    try:return _bootstrap_psutil_work(context,end)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,old_handler)
        if old_timer[0]:signal.setitimer(signal.ITIMER_REAL,max(.001,old_timer[0]-(time.monotonic()-started)),old_timer[1])
