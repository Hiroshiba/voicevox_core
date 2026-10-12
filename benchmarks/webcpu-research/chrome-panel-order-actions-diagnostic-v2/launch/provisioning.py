"""Narrow portable provisioning and baseline proof, guarded before execution.

No broad donor entrypoint is imported. Only the separately hash-pinned pure
source_proof module is materialized for its archive/IR predicates after admission.
All compiler children inherit the already production-owned provision phase.
"""
if __name__ == '__main__':
    raise SystemExit('SOURCE_ONLY_EXECUTION_BLOCK: launch.provisioning')

from launch.source_only import require_execution_admission


def canonical_sha256(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def apply_unified(originals, patch_text, positions=None):
    """Pure exact-context unified patching: no fuzzy offsets, tools or writes.

    Originals is a dict of POSIX relative path -> bytes, including absent new
    files as None. The returned dict contains only the explicitly patched files.
    """
    import re
    from launch.bootstrap_fetch import safe_name
    lines=patch_text.splitlines(keepends=True);i=0;result={}
    while i<len(lines):
        if not lines[i].startswith('--- '):i+=1;continue
        old=lines[i][4:].split('\t',1)[0].strip();i+=1
        if i>=len(lines) or not lines[i].startswith('+++ '):raise ValueError('bootstrap_patch_header')
        new=lines[i][4:].split('\t',1)[0].strip();i+=1
        if new=='/dev/null':raise ValueError('bootstrap_patch_deletion_not_admitted')
        name=new.split('/',1)[1];safe_name(name)
        if name in result or name not in originals:raise ValueError('bootstrap_patch_file_roster')
        if old!='/dev/null' and old.split('/',1)[1]!=name:raise ValueError('bootstrap_patch_rename')
        raw=originals[name]
        if (old=='/dev/null')!=(raw is None):raise ValueError('bootstrap_patch_existence')
        source=[] if raw is None else raw.decode('utf-8').splitlines(keepends=True)
        cursor=0;out=[];hunks=0
        while i<len(lines) and not lines[i].startswith(('--- ','diff --git ')):
            if not lines[i].startswith('@@ '):i+=1;continue
            m=re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',lines[i])
            if not m:raise ValueError('bootstrap_patch_hunk')
            old_start=int(m[1]);old_count=int(m[2] or 1);new_count=int(m[4] or 1)
            at=(positions[name][hunks] if positions is not None else (old_start-1 if old_count else old_start))
            if not cursor<=at<=len(source):raise ValueError('bootstrap_patch_offset')
            out.extend(source[cursor:at]);before=[];after=[];i+=1
            while len(before)<old_count or len(after)<new_count:
                if i>=len(lines):raise ValueError('bootstrap_patch_truncated')
                line=lines[i];i+=1
                if not line or line[0] not in ' +-':raise ValueError('bootstrap_patch_hunk_body')
                if line[0] in ' -':before.append(line[1:])
                if line[0] in ' +':after.append(line[1:])
                if i<len(lines) and lines[i].startswith('\\ No newline'):
                    if line[0] in ' -':before[-1]=before[-1].removesuffix('\n')
                    if line[0] in ' +':after[-1]=after[-1].removesuffix('\n')
                    i+=1
            if len(before)!=old_count or len(after)!=new_count or source[at:at+old_count]!=before:
                raise ValueError('bootstrap_patch_preimage_context')
            out.extend(after);cursor=at+old_count;hunks+=1
        if not hunks:raise ValueError('bootstrap_patch_no_hunks')
        out.extend(source[cursor:]);result[name]=''.join(out).encode('utf-8')
    if set(result)!=set(originals):raise ValueError('bootstrap_patch_unapplied_file')
    return result


def render(value, roots):
    if isinstance(value,list):return [render(v,roots) for v in value]
    if not isinstance(value,str):return value
    for key,path in roots.items():value=value.replace('${'+key+'}',str(path))
    if '${' in value:raise ValueError('bootstrap_unknown_path_token')
    return value


def allowed_monitor_links(context, prefix='provisioned/'):
    """Exact prospective SDK/dependency links, including before first extraction."""
    require_execution_admission('provisioning.allowed_monitor_links')
    import json
    from pathlib import Path
    if prefix not in ('','provisioned/'):raise ValueError('bootstrap_monitor_prefix')
    inputs=context.get('bootstrap_inputs')
    if inputs is None:inputs=json.loads((Path(context['roots']['SOURCE'])/'launch/bootstrap-inputs.json').read_text())
    result={}
    roots={'SDK':'sdk','DEPS':'deps','PROVISION':'','PACKAGES':'packages','ORT':'deps/ort-src'}
    for row in inputs['downloads']:
        destination=render(row['destination'],roots).strip('/')
        for link in row['symlinks']:
            name=prefix+destination+'/'+link['name']
            if name in result:raise ValueError('bootstrap_monitor_duplicate_link')
            result[name]=link['link_text']
    result[prefix+'deps/ort-src/cmake/external/emsdk']='../../../../sdk'
    return result


def _journal(root, value):
    require_execution_admission('provisioning._journal')
    import json
    from pathlib import Path
    path=Path(root)/'bootstrap-events.private.jsonl'
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
    if len(raw)>128*1024 or (path.exists() and path.stat().st_size+len(raw)>2*1024**2):raise ValueError('bootstrap_journal_budget')
    with path.open('ab') as stream:stream.write(raw)


def _verify_sdk_execution(inputs, sdk, deadline):
    require_execution_admission('provisioning._verify_sdk_execution')
    import os
    from pathlib import Path
    from launch.bootstrap_fetch import file_record
    sdk=Path(sdk);actual=set()
    for subroot in ('upstream','node/20.18.0_64bit'):
        for base,dirs,files in os.walk(sdk/subroot,followlinks=False):
            for name in files:
                path=Path(base)/name;relative=path.relative_to(sdk).as_posix()
                if path.is_symlink() or '__pycache__' in path.parts or path.suffix=='.pyc' or path.name=='.emsdk_version' or relative.startswith('upstream/emscripten/cache/'):continue
                actual.add(relative)
    expected={r['name']:r for r in inputs['sdk_execution_files']}
    if actual!=set(expected):raise ValueError('bootstrap_sdk_execution_roster')
    for name,row in expected.items():
        if file_record(sdk/name,deadline-30)!={'bytes':row['bytes'],'sha256':row['sha256']}:raise ValueError('bootstrap_sdk_execution_bytes')
    return {'complete_execution_file_roster':True,'files':len(expected),'identity':'previously_pinned_bytes_before_any_sdk_execution','cache_included':False}


def _check(deadline, root):
    require_execution_admission('provisioning._check')
    import os
    import shutil
    import time
    from pathlib import Path
    if time.monotonic()>=deadline-30:raise TimeoutError('bootstrap_phase_cleanup_reserve')
    if shutil.disk_usage(root).free<4*1024**3:raise ValueError('bootstrap_disk_reserve')
    total=0
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in files:
            p=Path(base)/name
            if not p.is_symlink():total+=p.stat().st_size
    if total>8*1024**3:raise ValueError('bootstrap_owned_tree_budget')


def _write(path, raw):
    require_execution_admission('provisioning._write')
    from pathlib import Path
    path=Path(path)
    if path.is_symlink() or path.exists():raise ValueError('bootstrap_fresh_output_required')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as stream:stream.write(raw)


def _patch(operation, roots, deadline):
    require_execution_admission('provisioning._patch')
    from pathlib import Path
    from launch.bootstrap_fetch import file_record
    patch=Path(render(operation['patch'],roots));target=Path(render(operation['target'],roots))
    if file_record(patch,deadline-30)['sha256']!=operation['patch_sha256']:raise ValueError('bootstrap_patch_pin')
    originals={}
    for row in operation['files']:
        p=target/row['name']
        if row['before_sha256'] is None:
            if p.exists() or p.is_symlink():raise ValueError('bootstrap_new_patch_file_exists')
            originals[row['name']]=None
        else:
            if p.is_symlink() or file_record(p,deadline-30)['sha256']!=row['before_sha256']:raise ValueError('bootstrap_patch_preimage_pin')
            originals[row['name']]=p.read_bytes()
    patched=apply_unified(originals,patch.read_text(),operation.get('hunk_positions'))
    import hashlib
    for row in operation['files']:
        raw=patched[row['name']]
        if hashlib.sha256(raw).hexdigest()!=row['after_sha256']:raise ValueError('bootstrap_patch_postimage_pin')
    for name,raw in patched.items():
        p=target/name;p.parent.mkdir(parents=True,exist_ok=True)
        if p.is_symlink():raise ValueError('bootstrap_patch_link')
        p.write_bytes(raw)
    return {'id':operation['id'],'kind':'exact_unified_patch','complete':True,'files':operation['files']}


def _command(spec, roots, env, deadline, journal):
    """No shell/session/daemon. Outer owned phase observes every descendant."""
    require_execution_admission('provisioning._command')
    import json
    import subprocess
    import time
    from pathlib import Path
    root=Path(roots['PROVISION']);_check(deadline,root)
    end=min(deadline-30,time.monotonic()+spec['wall_seconds'])
    argv=render(spec['argv'],roots);cwd=render(spec['cwd'],roots)
    log=root/'logs'/spec['id'];log.parent.mkdir(parents=True,exist_ok=True)
    receipt={'id':spec['id'],'argv':argv,'cwd':cwd,'started_monotonic':time.monotonic(),'deadline':end,
             'attempted':True,'acknowledged':False,'complete':False,'ownership':'inherits_outer_production_phase_no_new_session','operator_calls':0,'browser_launches':0}
    journal.append(receipt)
    _journal(root,{**receipt,'attempted':True,'acknowledged':False})
    with log.with_suffix('.stdout').open('xb') as out,log.with_suffix('.stderr').open('xb') as err:
        try:
            process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=False)
        except BaseException as error:
            receipt['first_failure']=type(error).__name__
            receipt['direct_child_started']=False
            _journal(root,{**receipt,'attempted':True,'acknowledged':False})
            raise
        try:
            while process.poll() is None:
                if time.monotonic()>=end:raise TimeoutError('bootstrap_command_deadline')
                if log.with_suffix('.stdout').stat().st_size+log.with_suffix('.stderr').stat().st_size>8*1024**2:
                    raise ValueError('bootstrap_command_log_budget')
                time.sleep(.05)
            receipt['returncode']=process.returncode
            if process.returncode:raise RuntimeError('bootstrap_command_failed:'+spec['id'])
        except BaseException as error:
            receipt['first_failure']=type(error).__name__
            _journal(root,{**receipt,'attempted':True,'acknowledged':False})
            if process.poll() is None:process.kill()
            try:process.wait(timeout=max(.001,min(5,deadline-time.monotonic())))
            except subprocess.TimeoutExpired:pass
            # Descendant cleanup and acknowledgement belong to the already
            # admitted outer owner. Never claim the direct-child kill sufficed.
            receipt['requires_outer_cleanup_acknowledgement']=True
            raise
    _check(deadline,root)
    receipt['wall_seconds']=time.monotonic()-receipt['started_monotonic']
    _journal(root,{**receipt,'complete':True,'acknowledged':True})
    receipt['complete']=True;receipt['acknowledged']=True
    return receipt


def _helper_proof(source, root, expected, deadline):
    require_execution_admission('provisioning._helper_proof')
    import importlib.util
    from pathlib import Path
    from launch.bootstrap_fetch import file_record
    reference=Path(source)/'launch/vendor-reference/vendor/source/source_proof.py.txt'
    if file_record(reference,deadline-30)!={'bytes':expected['bytes'],'sha256':expected['sha256']}:
        raise ValueError('bootstrap_source_proof_source_pin')
    target=Path(root)/'reviewed-helpers/source_proof.py';_write(target,reference.read_bytes())
    # This exact module only imports hashlib/re/pathlib and defines pure proofs.
    # It is not prepare_inplace_leaky_source, affine_toolchain or a broad runner.
    spec=importlib.util.spec_from_file_location('_panel_pinned_source_proof',target)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def _baseline(inputs, pins, roots, env, deadline, journal, proof):
    require_execution_admission('provisioning._baseline')
    import hashlib
    import json
    import re
    from pathlib import Path
    from launch.bootstrap_fetch import file_record
    base=Path(roots['BASELINE']);raw=Path(roots['RUNTIME_ARCHIVE']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=pins['pristine_runtime_archive_sha256']:raise ValueError('bootstrap_runtime_archive_pin')
    before=proof.archive_manifest(raw)
    if len(before)!=1438:raise ValueError('bootstrap_runtime_member_count')
    original={i:(name,data) for name,header,data,entry,i in proof.archive_entries(raw) if i in (514,757)}
    if original[514][0]!='activations.cc.o' or original[757][0]!='gemm-config.c.o':raise ValueError('bootstrap_baseline_member_names')
    if hashlib.sha256(original[514][1]).hexdigest()!=pins['original_activation_object_sha256']:raise ValueError('bootstrap_activation_member_pin')
    if hashlib.sha256(original[757][1]).hexdigest()!=inputs['baseline']['original_dispatch_sha256']:raise ValueError('bootstrap_dispatch_member_pin')
    _write(base/'activation.archived.bc',original[514][1]);_write(base/'dispatch.archived.bc',original[757][1])
    overlay=base/'overlay'/pins['source_header'];_write(overlay,(Path(roots['ORT'])/pins['source_header']).read_bytes())
    _patch(inputs['activation_overlay_patch'],{**roots,'OVERLAY':str(base/'overlay')},deadline)
    _write(base/'activation-overlay.json',json.dumps({'version':0,'use-external-names':False,'roots':[{'type':'file','name':str(Path(roots['ORT'])/pins['source_header']),'external-contents':str(overlay)}]},sort_keys=True).encode())
    commands={r['id']:r for r in inputs['commands']}
    for key in ('activation-archived-ir','activation-control','activation-control-ir','activation-candidate','activation-candidate-ir','dispatch-archived-ir'):
        _command(commands[key],roots,env,deadline,journal)
    control=proof.compare_control((base/'activation.archived.ll').read_text(),(base/'activation.control.ll').read_text(),pins)
    candidate=proof.compare_candidate((base/'activation.control.ll').read_text(),(base/'activation.candidate.ll').read_text(),pins)
    text=(base/'dispatch.archived.ll').read_text();function=re.search(r'define internal void @init_f32_gemm_config\(\).*?\n}',text,re.S)
    old='  %7 = icmp sgt i32 %6, 4';new='  %7 = icmp eq i32 0, 0'
    if function is None or text.count(old)!=1 or old not in function[0] or not re.search(r'%6 = tail call i32 @hardware_concurrency\(\)',function[0]):raise ValueError('bootstrap_dispatch_anchor')
    pairs=re.findall(r'  %\d+ = select i1 %7, ptr @(\w+), ptr @(\w+)',function[0])
    if len(pairs)!=12 or any(not a.endswith('_loadsplat') or b!=a.removesuffix('_loadsplat')+'_splat' for a,b in pairs):raise ValueError('bootstrap_dispatch_pointer_pairs')
    if any(x not in text for x in ('+simd128','+atomics','+exception-handling')) or '+relaxed-simd' in text:raise ValueError('bootstrap_dispatch_features')
    edited=text.replace(old,new);_write(base/'dispatch.loadsplat.ll',edited.encode())
    for key in ('dispatch-assemble','dispatch-roundtrip-ir'):_command(commands[key],roots,env,deadline,journal)
    if '\n'.join((base/'dispatch.verified.ll').read_text().splitlines()[1:])!='\n'.join(edited.splitlines()[1:]):raise ValueError('bootstrap_dispatch_roundtrip')
    forced=(base/'dispatch.loadsplat.bc').read_bytes()
    if hashlib.sha256(forced).hexdigest()!=inputs['baseline']['forced_dispatch_sha256']:raise ValueError('bootstrap_dispatch_forced_pin')
    replacements={514:(base/'activation.candidate.bc').read_bytes(),757:forced};parts=[b'!<arch>\n']
    for name,header,data,entry,index in proof.archive_entries(raw):
        if name in ('/','/SYM64/'):continue
        if index in replacements:
            value=replacements[index];parts.extend([header[:48]+str(len(value)).ljust(10).encode()+header[58:],value,b'\n' if len(value)%2 else b''])
        else:parts.append(entry)
    archive=base/'R-common-baseline.a';_write(archive,b''.join(parts));_command(commands['baseline-index'],roots,env,deadline,journal)
    after=proof.archive_manifest(archive.read_bytes())
    changed=[a['index'] for a,b in zip(before,after) if a!=b]
    if len(after)!=1438 or changed!=[514,757]:raise ValueError('bootstrap_baseline_scope')
    if file_record(Path(roots['ORT'])/pins['source_header'],deadline-30)['sha256']!=pins['original_source_header_sha256']:raise ValueError('bootstrap_original_activation_header_mutated')
    result={'passed':True,'modified_indices':[514,757],'changed_members':[514,757],'member_count':1438,'preserved_members':1436,
            'other_1436_payloads_preserved':True,'logical_control_verified':True,'logical_candidate_verified':True,'dispatch_verified':True,
            'source_input_pins_verified':True,'cross_host_byte_identity_claimed':False,'baseline_sha256':file_record(archive,deadline-30)['sha256'],
            'control_ir_sha256':control['normalized_IR_sha256'],'candidate_ir_sha256':candidate['normalized_IR_sha256'],
            'control_proof':control,'candidate_proof':candidate,'forced_dispatch_sha256':hashlib.sha256(forced).hexdigest()}
    inventory=[]
    for name,header,data,entry,index in proof.archive_entries(archive.read_bytes()):
        if index:inventory.append({'index':index,'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    _write(base/'baseline-proof.json',json.dumps(result,sort_keys=True).encode())
    return result,inventory


def _snapshot(roots, inputs, baseline_proof, deadline):
    require_execution_admission('provisioning._snapshot')
    import os
    from pathlib import Path
    from launch.bootstrap_fetch import file_record
    files=[];links=[]
    for token in ('SDK','DEPS','BASELINE'):
        root=Path(roots[token])
        for base,dirs,names in os.walk(root,followlinks=False):
            for name in sorted(dirs+names):
                path=Path(base)/name;relative=path.relative_to(root).as_posix()
                if path.is_symlink():
                    resolved=path.resolve(strict=True)
                    if not any(resolved.is_relative_to(Path(roots[t])) for t in ('SDK','DEPS','BASELINE')):raise ValueError('bootstrap_lock_link_escape')
                    links.append({'root':token,'name':relative,'target':os.readlink(path)})
                elif path.is_file():files.append({'root':token,'name':relative,**file_record(path,deadline-30)})
    return {'schema':'panel-portable-build-lock-v1','source_policy_sha256':canonical_sha256(inputs),
            'files':sorted(files,key=lambda r:(r['root'],r['name'])),'allowed_symlinks':sorted(links,key=lambda r:(r['root'],r['name'])),
            'baseline_proof':baseline_proof}


def _run(context, deadline, journal, progress):
    require_execution_admission('provisioning.run')
    import hashlib
    import json
    import os
    import shutil
    import sys
    import time
    from pathlib import Path
    from launch.bootstrap_fetch import acquire, extract, file_record, verify_roster
    root=Path(context['provision_root']);source=Path(context['roots']['SOURCE'])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():raise ValueError('bootstrap_owned_root_missing')
    if json.loads((root/'bootstrap-owner.json').read_text())!={'schema':'panel-owned-bootstrap-root-v1','source':str(source)}:raise ValueError('bootstrap_root_ownership')
    progress.update(operation='ownership',operation_id=None)
    initial=json.loads((root/'bootstrap-psutil-receipt.json').read_text())
    if initial.get('complete') is not True:raise ValueError('bootstrap_initial_psutil_incomplete')
    sys.dont_write_bytecode=True
    if sys.version_info[:2]!=(3,12) or sys.platform!='linux':raise ValueError('bootstrap_python_platform')
    if Path(context['roots']['PYTHON']).resolve()!=Path(sys.executable).resolve():raise ValueError('bootstrap_executing_python')
    progress.update(operation='helper_policy',operation_id=None)
    inputs=json.loads((source/'launch/bootstrap-inputs.json').read_text());helper=context['bootstrap_helper_lock']
    if inputs['helper_lock_sha256']!=file_record(source/'launch/bootstrap-helper-lock.json',deadline-30)['sha256']:raise ValueError('bootstrap_helper_policy_pin')
    if helper!=json.loads((source/'launch/bootstrap-helper-lock.json').read_text()):raise ValueError('bootstrap_helper_context_pin')
    roots={'PROVISION':str(root),'SOURCE':str(source),'PYTHON':str(sys.executable),'SDK':str(root/'sdk'),'DEPS':str(root/'deps'),
           'BASELINE':str(root/'baseline'),'ORT':str(root/'deps/ort-src'),'BUILD':str(root/'deps/build/Release'),
           'PACKAGES':str(root/'packages'),'HOME':str(root/'home'),'TMP':str(root/'tmp')}
    progress.update(operation='layout',operation_id=None)
    for name in ('baseline','packages','home','tmp','logs'):(root/name).mkdir()
    progress.update(operation='host_make',operation_id=None)
    host=context['host_tools']['make'];make=Path(host['path'])
    if str(make)!='/usr/bin/make' or file_record(make,deadline-30)!={'bytes':host['bytes'],'sha256':host['sha256']}:raise ValueError('bootstrap_host_make_identity')
    roots['MAKE']=str(make);downloads=[];extractions=[];_check(deadline,root)
    for row in inputs['downloads']:
        if row['id']=='wheel-psutil':continue
        target=root/'archives'/row['filename'];_journal(root,{'id':row['id'],'kind':'acquisition','attempted':True,'acknowledged':False})
        progress.update(operation='acquisition',operation_id=row['id'])
        downloads.append(acquire(row,target,deadline-30));_journal(root,{**downloads[-1],'kind':'acquisition','attempted':True,'acknowledged':True})
        progress.update(operation='extraction',operation_id=row['id'])
        destination=Path(render(row['destination'],roots));extractions.append(extract(row,target,destination,deadline-30))
        _check(deadline,root)
    # Only the exact requested package code reaches the isolated import parent.
    progress.update(operation='helper_install',operation_id=None)
    package_roots={}
    for name,spec in helper['runtime_helper_packages'].items():
        target=root/'deps/python'/('typing_extensions.py' if name=='typing_extensions' else name)
        if name!='psutil':
            wheel_name='typing-extensions' if name=='typing_extensions' else name
            original=root/'packages'/wheel_name/target.name
            if original.is_symlink():raise ValueError('bootstrap_runtime_package_link')
            original.rename(target)
        if spec.get('layout')=='standalone_module':
            rows=spec['package_files']
            if len(rows)!=1 or file_record(target,deadline-30)!={'bytes':rows[0]['bytes'],'sha256':rows[0]['sha256']}:raise ValueError('bootstrap_module_bytes')
        else:verify_roster(target,spec['package_files'],deadline-30)
        package_roots[name]=str(target)
    roots['CMAKE']=str(root/'packages/cmake/cmake/data/bin/cmake')
    progress.update(operation='sdk_verify',operation_id=None)
    for row in inputs['sdk_critical_files']:
        if file_record(Path(roots['SDK'])/row['name'],deadline-30)!={'bytes':row['bytes'],'sha256':row['sha256']}:raise ValueError('bootstrap_critical_sdk_bytes')
    sdk=Path(roots['SDK']);sdk_execution_proof=_verify_sdk_execution(inputs,sdk,deadline)
    progress.update(operation='sdk_configure',operation_id=None)
    config="LLVM_ROOT = "+repr(str(sdk/'upstream/bin'))+"\nBINARYEN_ROOT = "+repr(str(sdk/'upstream'))+"\nNODE_JS = ["+repr(str(sdk/'node/20.18.0_64bit/bin/node'))+"]\n"
    _write(sdk/'.emscripten',config.encode())
    # No installer is run. This explicit source symlink is owned and relocatable.
    link=Path(roots['ORT'])/'cmake/external/emsdk'
    if link.exists():
        if not link.is_dir() or any(link.iterdir()):raise ValueError('bootstrap_ort_sdk_placeholder')
        link.rmdir()
    link.symlink_to(os.path.relpath(sdk,link.parent),target_is_directory=True)
    for operation in inputs['patches']:
        progress.update(operation='patch',operation_id=operation['id'])
        journal.append(_patch(operation,roots,deadline))
    pins_path=source/'launch/vendor-reference/vendor/source/source_pins.json.txt'
    if file_record(pins_path,deadline-30)['sha256']!=inputs['source_pins_reference_sha256']:raise ValueError('bootstrap_source_pins_reference')
    pins=json.loads(pins_path.read_text())
    runtime_archives=list((root/'runtime').rglob('libonnxruntime_webassembly.a'))
    if len(runtime_archives)!=1:raise ValueError('bootstrap_runtime_archive_roster')
    roots['RUNTIME_ARCHIVE']=str(runtime_archives[0])
    # A narrow explicit environment: CMake has exact compiler/Make/Python paths.
    env={'PATH':str(Path(sys.executable).parent)+os.pathsep+str(sdk/'upstream/emscripten')+os.pathsep+'/usr/bin',
         'HOME':roots['HOME'],'TMPDIR':roots['TMP'],'TMP':roots['TMP'],'TEMP':roots['TMP'],'LANG':'C','LC_ALL':'C','TZ':'UTC',
         'PYTHONDONTWRITEBYTECODE':'1','EM_CONFIG':str(sdk/'.emscripten'),'EM_CACHE':str(sdk/'upstream/emscripten/cache'),
         'EMSDK':str(sdk),'EMSDK_PYTHON':str(sys.executable),'EMSDK_NODE':str(sdk/'node/20.18.0_64bit/bin/node'),
         'EMCC_CFLAGS':'-fwasm-exceptions -fno-fast-math -ffp-contract=off','EMCC_SKIP_SANITY_CHECK':'1',
         'EMCC_CORES':'1','BINARYEN_CORES':'1','EM_FROZEN_CACHE':'1','EM_PORTS':str(root/'ports'),
         'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','CMAKE_BUILD_PARALLEL_LEVEL':'2'}
    (root/'ports').mkdir();Path(roots['BUILD']).mkdir(parents=True)
    commands={r['id']:r for r in inputs['commands']}
    for key in ('configure-offline','generate-onnx-headers'):
        progress.update(operation='native_command',operation_id=key)
        _command(commands[key],roots,env,deadline,journal)
    progress.update(operation='source_closure',operation_id=None)
    aliases={'source':roots['ORT'],'sdk':roots['SDK'],'build':roots['BUILD'],'dependencies':roots['DEPS']}
    def expanded(value):return value.format(**aliases)
    # Source closure pins include every original activation dependency, generated
    # proto/config header and the exact candidate wasm SIMD resource header.
    source_rows=pins['dependencies']+pins['generated_header_provenance']+pins['candidate_additional_dependencies']
    for row in source_rows:
        p=Path(expanded(row['path']))
        if file_record(p,deadline-30)!={'bytes':row['bytes'],'sha256':row['sha256']}:raise ValueError('bootstrap_source_closure_pin:'+row['path'])
    import shlex
    response=Path(roots['BUILD'])/'CMakeFiles/onnxruntime_providers.dir/includes_CXX.rsp'
    observed=shlex.split(response.read_text());expected=[expanded(v) for v in pins['include_arguments']]
    trim=lambda s:s[:-2] if s.startswith('-I') and s.endswith('/.') else s
    if list(map(trim,observed))!=list(map(trim,expected)):raise ValueError('bootstrap_activation_include_order')
    entries=json.loads((Path(roots['BUILD'])/'compile_commands.json').read_text())
    selected=[e for e in entries if e['file']==str(Path(roots['ORT'])/pins['source_filename_suffix'])]
    if len(selected)!=1 or selected[0]['directory']!=roots['BUILD']:raise ValueError('bootstrap_activation_compile_database')
    actual=shlex.split(selected[0]['command']);actual[actual.index('-o')+1]='{object}'
    expected=[v.format(**aliases,object='{object}') for v in pins['compile_command']]
    if actual!=expected:raise ValueError('bootstrap_activation_compile_command')
    progress.update(operation='source_proof',operation_id=None)
    proof=_helper_proof(source,root,inputs['source_proof_reference'],deadline)
    progress.update(operation='baseline',operation_id=None)
    baseline,inventory=_baseline(inputs,pins,roots,env,deadline,journal,proof)
    # Frozen cache must remain frozen in the compile recipe; current generated
    # file bytes become an in-job identity, never a cross-host expected hash.
    progress.update(operation='snapshot',operation_id=None)
    lock=_snapshot(roots,inputs,baseline,deadline);_check(deadline,root)
    return {'schema':'panel-portable-bootstrap-receipt-v1','complete':True,'operator_calls':0,'browser_launches':0,
            'roots':{k:roots[k] for k in ('SDK','DEPS','BASELINE')},'runtime_helper_package_roots':package_roots,
            'portable_build_lock':lock,'baseline_inventory':inventory,'command_receipts':journal,
            'downloads':downloads,'extractions':extractions,'initial_psutil_receipt':initial,
            'source_policy_sha256':canonical_sha256(inputs),'source_closure_observations':len(source_rows),
            'sdk_distribution_identity':'archive identities acquired once; complete previously pinned non-cache execution-file roster and logical source closure verified before tools; no complete historical-cache-equivalence claim',
            'sdk_execution_proof':sdk_execution_proof,'host_make_identity':host,'host_provenance':'official Ubuntu preinstalled tool path, hash/size observed and sealed in-job; OS tree not pinned',
            'cross_host_byte_identity_claimed':False,'cache_frozen':True}


def run(context, deadline):
    require_execution_admission('provisioning.run')
    from tools.failure_diagnostics import diagnose, known_records
    progress={'operation':'preflight','operation_id':None};journal=[]
    try:
        return _run(context,deadline,journal,progress)
    except BaseException as error:
        if journal and journal[-1].get('attempted') is True and journal[-1].get('acknowledged') is not True:
            progress={'operation':'native_command','operation_id':journal[-1].get('id')}
        error.panel_failure_diagnostic=diagnose(error,'provision',**progress)
        error.panel_known_operations=known_records(journal)
        raise
