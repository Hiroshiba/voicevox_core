"""One bounded official Chrome acquisition after reviewed child admission."""
import sys
if __name__ == '__main__' and (not sys.flags.isolated or not sys.dont_write_bytecode):
    raise SystemExit('isolated_no_bytecode_required')


import sys
from pathlib import Path
_SOURCE_ROOT=Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:sys.path.insert(0,str(_SOURCE_ROOT))
from launch.source_only import require_execution_admission


def response_metadata(response, policy):
    """Validate synthetic or actual HTTP metadata without performing I/O."""
    if response.status != 200:raise ValueError('browser_http_status')
    url=response.geturl()
    if url not in [policy['source_url'],*policy['allowed_redirect_urls']]:raise ValueError('browser_response_source')
    def one(name, default=None):
        values=response.headers.get_all(name,[])
        if len(values) != 1:
            if not values and default is not None:return default
            raise ValueError('browser_duplicate_or_missing_header')
        return values[0]
    length=one('Content-Length')
    if not length.isdecimal() or not 0 < int(length) <= policy['limits']['maximum_compressed_bytes']:raise ValueError('browser_content_length')
    content_type=one('Content-Type').split(';',1)[0].strip().lower()
    if content_type not in ('application/zip','application/x-zip-compressed','application/octet-stream'):raise ValueError('browser_content_type')
    if one('Content-Encoding','identity').lower() not in ('identity',''):raise ValueError('browser_transfer_transform')
    etag=one('ETag')
    if not etag or len(etag)>512:raise ValueError('browser_etag_missing')
    return {'url':url,'status':200,'content_length':int(length),'content_type':content_type,'etag':etag}


def validate_zip_method(method):
    # ZIP_STORED and ZIP_DEFLATED only; never load optional archive codecs.
    if type(method) is not int or method not in (0,8):raise ValueError('browser_archive_codec')


def acquire(policy, destination, deadline):
    require_execution_admission('browser_acquisition.acquire')
    import hashlib
    import json
    import os
    import stat
    import time
    import urllib.request
    import zipfile
    from pathlib import Path, PurePosixPath
    from tools.build_driver import validate_browser_policy, validate_browser_provisioning_receipt, canonical_sha256
    validate_browser_policy(policy)
    root=Path(destination)
    if root.exists():raise ValueError('browser_acquisition_one_fresh_attempt')
    root.mkdir(parents=True)
    limits=policy['limits'];end=min(deadline,time.monotonic()+limits['wall_seconds'])
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != policy['helper_sha256']:
        raise ValueError('browser_acquisition_helper_drift')
    redirects=[]
    class ExactRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if newurl not in policy['allowed_redirect_urls'] or sum(row['method'] == req.get_method() for row in redirects) >= len(policy['allowed_redirect_urls']):
                raise ValueError('browser_redirect_not_admitted')
            redirects.append({'from_url':req.full_url,'to_url':newurl,'status':code,'method':req.get_method()})
            return urllib.request.Request(newurl,method=req.get_method(),headers=dict(req.header_items()))
    opener=urllib.request.build_opener(ExactRedirect())
    def open_request(method):
        if time.monotonic() >= end:raise ValueError('browser_acquisition_deadline')
        request=urllib.request.Request(policy['source_url'],method=method,headers={'Accept-Encoding':'identity','User-Agent':'chrome-panel-source-reviewed-acquisition/1'})
        return opener.open(request,timeout=min(limits['socket_timeout_seconds'],max(.001,end-time.monotonic())))
    with open_request('HEAD') as response:head=response_metadata(response,policy)
    archive_path=root/'browser-distribution.zip';checksum=hashlib.sha256();total=0
    with open_request('GET') as response,archive_path.open('xb') as output:
        get=response_metadata(response,policy)
        if head != get:raise ValueError('browser_head_get_identity_mismatch')
        while True:
            if time.monotonic() >= end:raise ValueError('browser_acquisition_deadline')
            block=response.read(limits['read_chunk_bytes'])
            if not block:break
            total+=len(block)
            if total > get['content_length'] or total > limits['maximum_compressed_bytes']:raise ValueError('browser_archive_size_overrun')
            output.write(block);checksum.update(block)
    if total != get['content_length']:raise ValueError('browser_archive_truncated')
    os.chmod(archive_path,0o600)
    install=root/'installation';install.mkdir();rows=[];names=set();expanded=0
    with zipfile.ZipFile(archive_path) as archive:
        members=archive.infolist()
        if not 0 < len(members) <= limits['maximum_zip_entries']:raise ValueError('browser_archive_entries')
        for member in members:
            if time.monotonic() >= end:raise ValueError('browser_acquisition_deadline')
            name=member.filename;p=PurePosixPath(name)
            if not name or p.is_absolute() or '..' in p.parts or '\\' in name or '\x00' in name or p.as_posix() != name.rstrip('/') or name in names:
                raise ValueError('browser_archive_member_path')
            names.add(name)
            validate_zip_method(member.compress_type)
            if member.flag_bits & 1:raise ValueError('browser_encrypted_member')
            mode=(member.external_attr>>16)&0xffff
            if stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR):raise ValueError('browser_nonregular_member')
            if mode & 0o7000:raise ValueError('browser_special_permissions')
            target=install/name
            if member.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            if not 0 <= member.file_size <= limits['maximum_single_member_bytes']:raise ValueError('browser_member_size')
            expanded+=member.file_size
            if expanded > limits['maximum_total_uncompressed_bytes']:raise ValueError('browser_installation_size')
            target.parent.mkdir(parents=True,exist_ok=True);member_hash=hashlib.sha256();count=0
            with archive.open(member) as source,target.open('xb') as output:
                while True:
                    if time.monotonic() >= end:raise ValueError('browser_acquisition_deadline')
                    block=source.read(limits['read_chunk_bytes'])
                    if not block:break
                    count+=len(block)
                    if count > member.file_size:raise ValueError('browser_member_overrun')
                    output.write(block);member_hash.update(block)
            if count != member.file_size:raise ValueError('browser_member_truncated')
            final_mode=0o755 if mode & 0o111 else 0o644
            os.chmod(target,final_mode)
            rows.append({'name':name,'bytes':count,'sha256':member_hash.hexdigest(),'mode':final_mode})
    rows.sort(key=lambda r:r['name'])
    executable=install/policy['executable_name']
    with executable.open('rb') as stream:header=stream.read(64)
    if len(header) != 64 or header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':raise ValueError('browser_executable_format')
    manifest={'schema':'panel-browser-installation-v1','executable_name':policy['executable_name'],'files':rows}
    result={'schema':'panel-browser-provisioning-receipt-v1','status':'complete','identity_scope':'acquired_once_and_pinned_within_ci_job','source_policy_sha256':canonical_sha256(policy),'helper_sha256':policy['helper_sha256'],'acquisitions':1,'head_requests':1,'get_requests':1,'redirects':redirects,'head':head,'get':get,'archive':{'name':'browser-distribution.zip','bytes':total,'sha256':checksum.hexdigest()},'installation':manifest,'installation_sha256':canonical_sha256(manifest),'executable_format':{'format':'ELF','bits':64,'endianness':'little','machine':62},'browser_launches':0,'operator_calls':0,'first_failure':None}
    validate_browser_provisioning_receipt(result,policy)
    raw=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>limits['maximum_scalar_receipt_bytes']:raise ValueError('browser_acquisition_receipt_budget')
    with (root/'browser-provisioning.json').open('xb') as output:output.write(raw)
    if time.monotonic() >= end:raise ValueError('browser_acquisition_deadline')
    return result


def main(argv=None):
    require_execution_admission('browser_acquisition.main')
    import argparse
    import json
    parser=argparse.ArgumentParser()
    parser.add_argument('--policy',required=True);parser.add_argument('--destination',required=True);parser.add_argument('--deadline',type=float,required=True)
    args=parser.parse_args(argv)
    return acquire(json.loads(Path(args.policy).read_text()),args.destination,args.deadline)


if __name__ == '__main__':
    main()
