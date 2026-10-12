"""Portable build-recipe inspection and exact whole-asset validation.

No compilation implementation is activated in this release. Pure functions below
inspect source manifests and synthetic byte inventories without invoking tools.
"""
# This check deliberately precedes ALL imports, including standard-library ones.
if __name__ == "__main__":
    raise SystemExit("SOURCE_ONLY_EXECUTION_BLOCK: tools.build_driver")

import hashlib
import json
import re
from pathlib import PurePosixPath

from launch.source_only import require_execution_admission

ARMS = ("R", "A", "B", "Q", "P")
SHA = re.compile(r"^[0-9a-f]{64}$")


class BuildGateRejected(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise BuildGateRejected(reason)


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_asset_manifest(manifest):
    """Require admitted exact names, sizes, complete SHA256s and complete roster.

    The SOURCE_ONLY placeholder intentionally fails. Admission never copies actual
    hashes into expected hashes and never normalizes bytes or accepts subsets.
    """
    _require(type(manifest) is dict, "asset_manifest_not_object")
    _require(set(manifest) == {"schema", "admission", "assets", "logical_names"}, "asset_manifest_fields")
    _require(manifest["schema"] == "panel-assets-v1", "asset_manifest_schema")
    _require(manifest["admission"] == "independently_reviewed_complete_assets", "assets_unadmitted")
    assets = manifest["assets"]
    names = manifest["logical_names"]
    _require(type(assets) is list and type(names) is list and bool(assets), "asset_roster_missing")
    _require(names == sorted(set(names)), "logical_names_not_unique_sorted")
    _require(len(assets) == len(names), "asset_roster_count")
    observed = []
    for item in assets:
        _require(type(item) is dict and set(item) == {"name", "bytes", "sha256", "role", "arm"}, "asset_fields")
        name = item["name"]
        _require(type(name) is str and name and not PurePosixPath(name).is_absolute(), "asset_name_absolute")
        _require(PurePosixPath(name).as_posix() == name and ".." not in PurePosixPath(name).parts and "\\" not in name, "asset_name_unsafe")
        _require(type(item["bytes"]) is int and item["bytes"] > 0, "asset_size_invalid")
        _require(type(item["sha256"]) is str and bool(SHA.fullmatch(item["sha256"])), "asset_hash_invalid")
        _require(item["role"] in {"module_js", "module_wasm", "backing_worker", "controller", "operator_worker", "html"}, "asset_role_invalid")
        _require(item["arm"] in (*ARMS, None), "asset_arm_invalid")
        observed.append(name)
    _require(observed == names, "asset_order_or_names_mismatch")
    for arm in ARMS:
        for role in ("module_js", "module_wasm"):
            _require(sum(x["arm"] == arm and x["role"] == role for x in assets) == 1, "missing_or_duplicate_arm_asset")
    for role in ("controller", "operator_worker", "html"):
        _require(sum(x["role"] == role and x["arm"] is None for x in assets) == 1, "missing_host_asset")
    # Embedded Emscripten backing-worker support is permissible only if the exact
    # emitted review declares it. Any external worker is a separately listed file.
    return canonical_sha256(manifest)


def compare_asset_manifests(expected, actual):
    validate_asset_manifest(expected)
    validate_asset_manifest(actual)
    _require(actual == expected, "build_identity_mismatch")
    return {"status": "exact_whole_file_identity", "manifest_sha256": canonical_sha256(expected), "asset_count": len(expected["assets"])}


def inventory_supplied_bytes(expected, supplied_bytes):
    """Hash bytes already supplied by a reviewer/test; no filesystem discovery."""
    validate_asset_manifest(expected)
    _require(type(supplied_bytes) is dict and set(supplied_bytes) == set(expected["logical_names"]), "asset_names_missing_or_extra")
    result = json.loads(json.dumps(expected))
    for row in result["assets"]:
        value = supplied_bytes[row["name"]]
        _require(type(value) is bytes, "asset_is_not_complete_bytes")
        row["bytes"] = len(value)
        row["sha256"] = hashlib.sha256(value).hexdigest()
    return compare_asset_manifests(expected, result)


def validate_recipe(recipe):
    _require(recipe.get("schema") == "panel-portable-build-recipe-v1", "recipe_schema")
    _require(recipe.get("execution_status") == "SOURCE_ONLY_EXECUTION_BLOCK", "recipe_release_status")
    _require(recipe.get("attempts") == 1 and recipe.get("post_build_normalization") is False, "recipe_retry_or_normalization")
    _require(recipe.get("arm_macro") == {"R": None, "A": None, "B": 1, "Q": 3, "P": 2}, "recipe_arm_macro")
    _require(recipe.get("environment", {}).get("EM_FROZEN_CACHE") == "1", "mutable_toolchain_cache")
    _require(recipe.get("environment", {}).get("PATH") == "", "ambient_executable_lookup")
    forbidden = ("-ffast-math", "-mrelaxed-simd", "-ffp-contract=fast", "-march=native", "ENVIRONMENT=node")
    commands = recipe.get("commands")
    _require(type(commands) is list and commands, "recipe_commands_missing")
    ids = set()
    for command in commands:
        _require(command["id"] not in ids, "duplicate_build_command")
        ids.add(command["id"])
        joined = " ".join(command["argv"])
        _require(not any(x in joined for x in forbidden), "recipe_forbidden_flag")
        _require("/workspace/" not in joined and "/opt/" not in joined, "recipe_private_absolute_path")
        if command["kind"] in ("compiler", "linker") and "emcc.py" in joined:
            _require("-fno-fast-math" in command["argv"] and "-ffp-contract=off" in command["argv"], "recipe_missing_strict_flags")
    _require(sum(x["id"].startswith("link_") for x in commands) == 5, "recipe_final_link_count")
    return {"status": "source_recipe_structurally_valid", "commands": len(commands), "execution_admitted": False}


def run(*args, **kwargs):
    require_execution_admission("build_driver.run")
    return compile_once(*args, **kwargs)


def main(*args, **kwargs):
    require_execution_admission("build_driver.main")
    return run(*args, **kwargs)


def verify_input_lock(lock, roots):
    """Read-only complete byte-lock check; no optional imports or execution."""
    from pathlib import Path
    if lock.get('schema') == 'panel-portable-build-lock-v1':
        _require(set(lock)=={'schema','source_policy_sha256','files','allowed_symlinks','baseline_proof'},'portable_lock_fields')
        policy=json.loads((Path(roots['SOURCE'])/'launch/bootstrap-inputs.json').read_text())
        _require(lock['source_policy_sha256']==canonical_sha256(policy),'portable_lock_policy')
        proof=lock['baseline_proof']
        _require(proof['passed'] is True and proof['changed_members']==[514,757] and type(proof['member_count']) is int and proof['member_count']==1438 and proof['other_1436_payloads_preserved'] is True and proof['source_input_pins_verified'] is True and proof['cross_host_byte_identity_claimed'] is False,'portable_baseline_proof')
        expected={token:{} for token in ('SDK','DEPS','BASELINE')}
        for row in lock['files']:
            _require(set(row)=={'root','name','bytes','sha256'} and row['root'] in expected,'portable_lock_row')
            name=PurePosixPath(row['name']);_require(not name.is_absolute() and '..' not in name.parts and name.as_posix()==row['name'],'portable_lock_path')
            _require(row['name'] not in expected[row['root']],'portable_duplicate_file');expected[row['root']][row['name']]=row
        links={token:{} for token in expected}
        for row in lock['allowed_symlinks']:
            _require(set(row)=={'root','name','target'} and row['root'] in links,'portable_link_row')
            _require(row['name'] not in links[row['root']],'portable_duplicate_link');links[row['root']][row['name']]=row['target']
        for token,rows in expected.items():
            root=Path(roots[token]);seen=set();seen_links={}
            for target in root.rglob('*'):
                name=target.relative_to(root).as_posix()
                if target.is_symlink():
                    seen_links[name]=str(target.readlink())
                    _require(target.resolve().is_relative_to(Path(roots['SDK'])) or target.resolve().is_relative_to(root),'portable_link_escape')
                elif target.is_file():
                    seen.add(name);_require(name in rows,'portable_unlisted_file');row=rows[name]
                    _require(type(row['bytes']) is int and target.stat().st_size==row['bytes'],'portable_input_size')
                    with target.open('rb') as stream:
                        checksum=hashlib.sha256()
                        for chunk in iter(lambda:stream.read(1048576),b''):checksum.update(chunk)
                    _require(checksum.hexdigest()==row['sha256'],'portable_input_byte_drift')
                else:_require(target.is_dir(),'portable_special_file')
            _require(seen==set(rows) and seen_links==links[token],'portable_input_roster')
        baseline=Path(roots['BASELINE'])/'R-common-baseline.a'
        _require(hashlib.sha256(baseline.read_bytes()).hexdigest()==proof['baseline_sha256'],'portable_baseline_byte_drift')
        return canonical_sha256(lock)
    _require(type(lock) is dict and set(lock) == {'schema','admission','files','trees'}, 'input_lock_fields')
    _require(lock['schema'] == 'panel-build-input-lock-v1' and lock['admission'] == 'independently_reviewed', 'input_lock_unadmitted')
    seen = set()
    for row in lock['files']:
        _require(set(row) == {'root','name','bytes','sha256'}, 'input_lock_row')
        name = PurePosixPath(row['name'])
        _require(row['root'] in roots and not name.is_absolute() and '..' not in name.parts and name.as_posix() == row['name'], 'input_lock_path')
        key = (row['root'],row['name']); _require(key not in seen,'duplicate_input'); seen.add(key)
        root = Path(roots[row['root']]).resolve(); target = root/row['name']
        _require(target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(root), 'input_missing_or_redirected')
        for ancestor in target.parents:
            if ancestor == root: break
            _require(not ancestor.is_symlink(),'input_ancestor_symlink')
        _require(target.stat().st_size == row['bytes'] and hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256'], 'input_byte_drift')
    _require(set(lock['trees']) == {'SOURCE','SDK','DEPS','BASELINE'}, 'complete_lock_roots')
    for token in lock['trees']:
        base = Path(roots[token]); actual = {p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file()}
        _require(actual == {name for owner,name in seen if owner == token}, 'input_tree_extra_or_missing')
    return canonical_sha256(lock)


def render_tokens(value, roots):
    if isinstance(value, list): return [render_tokens(item,roots) for item in value]
    if isinstance(value, dict): return {key:render_tokens(item,roots) for key,item in value.items()}
    if not isinstance(value,str): return value
    for key,path in roots.items(): value = value.replace('${'+key+'}',str(path))
    _require('${' not in value,'unknown_recipe_path_token')
    return value


ADMISSION_ENV_FIELDS=('GITHUB_ACTIONS','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB','GITHUB_SHA','RUNNER_TEMP')
def python_helper_environment(compiler_env,phase_env):
    """Transport observed descriptor-bound job facts only to Python ownership helpers.

    compiler_process still execs with the unchanged exact recipe environment.
    """
    _require(all(type(phase_env.get(key)) is str and phase_env[key] for key in ADMISSION_ENV_FIELDS),'helper_admission_environment_missing')
    return {**compiler_env,**{key:phase_env[key] for key in ADMISSION_ENV_FIELDS}}


def supervise_command(command, cwd, env, deadline, output_root, limits, journal):
    """One production-owned command with strict observed resource monitoring.

    Uses the pinned all-thread subreaper/pidfd donor, never a process-group-only
    fallback. Exited-process CPU and transient sampling limits are disclosed.
    """
    require_execution_admission('build_driver.supervise_command')
    import time
    import shutil
    from pathlib import Path
    _require('bootstrap_helper_lock' in limits and 'runtime_helper_package_roots' in limits,'supervisor_helper_admission_missing')
    verify_runtime_helpers(limits['bootstrap_helper_lock'],limits['runtime_helper_package_roots'],ownership_only=True)
    verify_helper_import_routes(limits['runtime_helper_package_roots'],ownership_only=True)
    import psutil
    from tools import process_guard
    from browser.vendor import strict_resources
    output_root = Path(output_root)
    process_guard.assert_no_unresolved_owners()
    end = min(deadline-30,time.monotonic()+limits['wall_seconds']-30)
    _require(time.monotonic() < end,'command_cleanup_reserve')
    stdout = output_root/f'command-{len(journal)}.stdout.private'; stderr = output_root/f'command-{len(journal)}.stderr.private'
    started = time.monotonic()
    host_initial = psutil.swap_memory()
    note = {'attempted':True,'acknowledged':False,'returncode':None,'first_failure':None,'snapshots':0,'peak_rss_bytes':0,'peak_artifact_bytes':0,'samples':[],'startup_gate_released':False,'min_available_disk_bytes':None,'stage':limits.get('stage'),'command_id':limits.get('command_id')}
    cpu_max = {}
    swap_max = {}
    _require(psutil.virtual_memory().available >= limits.get('minimum_host_available_bytes',1024**3),'initial_host_available')
    journal.append(note) # Record attempted command before constructor/launch.
    owner = None
    with stdout.open('xb') as out, stderr.open('xb') as err:
        try:
            owner = process_guard.OwnedProcess(command,cwd=cwd,deadline=end,cleanup_deadline=deadline,env=env,stdout=out,stderr=err,cpu_limit=limits['per_process_cpu_seconds'])
            while owner.receipt is None:
                _require(time.monotonic() < end,'command_wall_budget')
                owner._receive(end)
                if owner.receipt is not None: break
                if owner.identity is None: continue
                _require(owner.owned_enumeration_verified,'missing_all_thread_ownership')
                generations = strict_resources.enumerate_tree(owner.identity[0],owner.identity[1],psutil.Process,Path('/proc'))
                rss = 0
                for pid,birth in generations.items():
                    p = psutil.Process(pid); _require(p.create_time() == birth,'command_generation_changed')
                    cpu=p.cpu_times(); used=cpu.user+cpu.system
                    _require(used <= limits['per_process_cpu_seconds'],'command_cpu_budget')
                    generation=(pid,birth); _require(used >= cpu_max.get(generation,0),'command_cpu_nonmonotone')
                    cpu_max[generation]=used
                    rss += p.memory_info().rss
                    status=Path('/proc')/str(pid)/'status'
                    fields=dict(line.split(':',1) for line in status.read_text().splitlines() if ':' in line)
                    _require('VmSwap' in fields,'target_swap_observation_missing')
                    swap_text=fields['VmSwap'].strip().split(); _require(len(swap_text) == 2 and swap_text[1] == 'kB' and swap_text[0].isdigit(),'target_swap_observation_malformed')
                    target_swap=int(swap_text[0])*1024
                    _require(target_swap <= swap_max.get(generation,target_swap),'target_swap_growth')
                    swap_max[generation]=target_swap
                    _require(psutil.Process(pid).create_time() == birth,'command_generation_changed_after_observation')
                after_generations=strict_resources.enumerate_tree(owner.identity[0],owner.identity[1],psutil.Process,Path('/proc'))
                _require(after_generations == generations,'command_tree_changed_during_observation')
                _require(rss <= limits['observed_rss_bytes'],'command_rss_budget')
                sizes = 0
                for path in output_root.rglob('*'):
                    if path.is_symlink():
                        allowed=limits.get('allowed_symlinks',{})
                        relative=path.relative_to(output_root).as_posix()
                        _require(relative in allowed and str(path.readlink())==allowed[relative] and path.resolve().is_relative_to(output_root.resolve()),'output_symlink')
                    elif path.is_file(): sizes += path.stat().st_size
                _require(sizes <= limits['observed_new_artifacts_bytes'],'command_artifact_budget')
                if limits.get('max_command_log_bytes'):
                    _require(stdout.stat().st_size+stderr.stat().st_size <= limits['max_command_log_bytes'],'command_log_budget')
                free_disk=shutil.disk_usage(output_root).free
                note['min_available_disk_bytes']=free_disk if note['min_available_disk_bytes'] is None else min(note['min_available_disk_bytes'],free_disk)
                _require(free_disk >= limits['minimum_available_disk_bytes'],'disk_reserve')
                available=psutil.virtual_memory().available; swap=psutil.swap_memory()
                _require(available >= limits.get('minimum_host_available_bytes',1024**3),'host_available_budget')
                _require(swap.sin == host_initial.sin and swap.sout == host_initial.sout,'whole_phase_host_swap_io')
                tree_cpu=sum(cpu_max.values()); _require(tree_cpu <= limits.get('observed_tree_cpu_seconds',limits['per_process_cpu_seconds']),'tree_cpu_budget')
                note['samples'].append({'monotonic_seconds':time.monotonic(),'rss_bytes':rss,'artifact_bytes':sizes,'host_available_bytes':available,'tree_cpu_seconds':tree_cpu,'generations':len(generations),'target_swap_growth_bytes':0,'host_swap_in_delta_bytes':swap.sin-host_initial.sin,'host_swap_out_delta_bytes':swap.sout-host_initial.sout})
                _require(len(note['samples']) <= 60000,'resource_sample_budget')
                note.update(snapshots=note['snapshots']+1,peak_rss_bytes=max(note['peak_rss_bytes'],rss),peak_artifact_bytes=max(note['peak_artifact_bytes'],sizes))
                if limits.get('ready_path') and not note['startup_gate_released'] and Path(limits['ready_path']).is_file():
                    ready=json.loads(Path(limits['ready_path']).read_text())
                    _require(ready == {'stage':limits['stage'],'cpu_limit':limits['per_process_cpu_seconds'],'deadline':deadline},'phase_start_handshake')
                    with Path(limits['start_gate_path']).open('x') as stream: json.dump({'stage':limits['stage'],'owned_tree_observed':True},stream)
                    note['startup_gate_released']=True
            cleanup=owner.wait(deadline=deadline)
            note.update(acknowledged=True,returncode=cleanup['returncode'],cleanup=cleanup,wall_seconds=time.monotonic()-started,tree_cpu_seconds=sum(cpu_max.values()))
            final_host=psutil.virtual_memory().available; final_swap=psutil.swap_memory()
            _require(final_host >= limits.get('minimum_host_available_bytes',1024**3),'cleanup_host_available')
            _require(final_swap.sin == host_initial.sin and final_swap.sout == host_initial.sout,'cleanup_host_swap_io')
            _require(cleanup['confirmed'] is True and cleanup['remaining_processes'] == 0 and cleanup['failure'] is None and cleanup['returncode'] == 0,'command_failed_or_cleanup_unknown')
            _require(note['snapshots'] > 0,'command_missing_observation')
            return note
        except BaseException as error:
            note['first_failure'] = type(error).__name__
            if owner is not None: note['cleanup']=owner.close(deadline=deadline)
            try:
                from tools.failure_diagnostics import diagnose
                note['failure_diagnostic']=diagnose(error,limits.get('stage'),'owned_command',limits.get('command_id'))
            except BaseException:
                note['diagnostic_unavailable']=True
            raise
        finally:
            if owner is not None: owner.release_streams()
            process_guard.assert_no_unresolved_owners()


def provision(plan, context, deadline, journal):
    require_execution_admission('build_driver.provision')
    import shutil
    import time
    from pathlib import Path
    _require(plan['schema'] == 'panel-provisioning-v1' and plan['admission'] == 'independently_reviewed', 'provision_unadmitted')
    _require(plan['observed_peak_disk_ceiling_bytes'] == 8*1024**3 and plan['minimum_available_disk_bytes'] == 4*1024**3,'provision_budget_changed')
    _require(plan['input_lock_sha256']==canonical_sha256(context['bootstrap_inputs']) and plan['commands']==context['bootstrap_inputs']['commands'],'provision_source_recipe_binding')
    from launch.provisioning import run as provision_fresh
    out=Path(context['provision_root'])
    _require(out.is_dir(),'initial_ownership_bootstrap_missing')
    _require(shutil.disk_usage(out).free >= 4*1024**3,'provision_disk_reserve')
    started=time.monotonic()
    limits={'bootstrap_helper_lock':context['bootstrap_helper_lock'],'runtime_helper_package_roots':context['runtime_helper_package_roots'],'wall_seconds':2700,'per_process_cpu_seconds':2700,'observed_rss_bytes':3*1024**3,'observed_new_artifacts_bytes':8*1024**3,'minimum_available_disk_bytes':4*1024**3,'stage':'provision'}
    bootstrap=provision_fresh(context,deadline)
    _require(bootstrap['complete'] is True,'fresh_bootstrap_incomplete')
    context['build_input_lock']=bootstrap['portable_build_lock'];context['baseline_members']=bootstrap['baseline_inventory']
    verify_input_lock(context['build_input_lock'],context['roots'])
    # A separate production-owned helper enforces the full acquisition wall
    # deadline even if a server drips bytes without tripping a socket timeout.
    policy=context['browser_acquisition_policy'];validate_browser_policy(policy)
    policy_path=out/'browser-acquisition-policy.private.json';policy_path.write_text(json.dumps(policy,sort_keys=True))
    helper_end=min(deadline,time.monotonic()+policy['limits']['wall_seconds'])
    from launch.source_only import child_python_command
    command=child_python_command(Path(context['roots']['SOURCE'])/'launch/browser_acquisition.py',['--policy',str(policy_path),'--destination',str(out/'browser'),'--deadline',str(helper_end)])
    from launch.provisioning import allowed_monitor_links
    limits['allowed_symlinks']=allowed_monitor_links(context,prefix='')
    browser_limits={**limits,'command_id':'official_browser_acquisition','wall_seconds':180,'per_process_cpu_seconds':180,'observed_tree_cpu_seconds':180}
    supervise_command(command,out,context['phase_environment'],helper_end,out,browser_limits,journal)
    browser_receipt=json.loads((out/'browser/browser-provisioning.json').read_text())
    verify_retained_browser(browser_receipt,context['browser_acquisition_policy'],out/'browser/browser-distribution.zip',out/'browser/installation')
    return {'portable_build_lock':bootstrap['portable_build_lock'],'baseline_inventory':bootstrap['baseline_inventory'],'bootstrap':{k:v for k,v in bootstrap.items() if k not in ('portable_build_lock','baseline_inventory')},'browser_provisioning_receipt':browser_receipt,'schema':'panel-provisioning-receipt-v1','complete':True,'wall_seconds':time.monotonic()-started,'browser_launches':0,'operator_calls':0,'input_lock_sha256':canonical_sha256(context['build_input_lock']),'commands':journal}


def compile_once(recipe, context, deadline, journal):
    require_execution_admission('build_driver.compile_once')
    import time
    from pathlib import Path
    from launch.archive_recipe import run as archive_run
    validate_recipe(recipe)
    verify_input_lock(context['build_input_lock'],context['roots'])
    out=Path(context['roots']['OUT']); _require(not out.exists(),'compile_fresh_root'); out.mkdir(parents=True)
    for name in ('home','ports','tmp'): (out/name).mkdir()
    for arm in ARMS: (out/'assets'/arm).mkdir(parents=True)
    env=render_tokens(recipe['environment'],context['roots'])
    started=time.monotonic(); limits={'bootstrap_helper_lock':context['bootstrap_helper_lock'],'runtime_helper_package_roots':context['runtime_helper_package_roots'],'wall_seconds':600,'per_process_cpu_seconds':480,'observed_rss_bytes':2*1024**3,'observed_new_artifacts_bytes':768*1024**2,'minimum_available_disk_bytes':4*1024**3,'max_command_log_bytes':1024**2}
    for item in recipe['commands']:
        _require(time.monotonic() < deadline-30,'compile_deadline')
        command=render_tokens(item,context['roots'])
        if command['kind'] == 'archive_prepare':
            archive_run(Path(context['roots']['BASELINE'])/'R-common-baseline.a',out,context['baseline_members'])
            journal.append({'id':item['id'],'kind':'pure_archive_construction','complete':True})
        else:
            # Serial helper group, private bounded logs; no post-build rewriting.
            logroot=out/'logs'/item['id']; logroot.mkdir(parents=True)
            oldlen=len(journal)
            spec=out/'logs'/item['id']/'command-spec.private.json'
            spec.write_text(json.dumps({'id':item['id'],'argv':command['argv'],'cwd':command['cwd'],'env':env},sort_keys=True))
            from launch.source_only import child_python_command
            wrapped=child_python_command(Path(context['roots']['SOURCE'])/'launch/compiler_process.py',['--spec',str(spec)])
            supervise_command(wrapped,command['cwd'],python_helper_environment(env,context['phase_environment']),deadline,out,{**limits,'stage':'compile','command_id':item['id']},journal)
            journal[oldlen]['id']=item['id']
        for output in command['outputs']: _require(Path(output).is_file(),'expected_compile_output_missing')
    for arm in ARMS:
        # Relocate unmodified nonloaded review-only symbols, not executable bytes.
        (out/'assets'/arm/'screen.js.symbols').rename(out/f'{arm}.js.symbols')
    for name in ('index.html','controller.js','worker.js'):
        target=out/'assets'/name; target.write_bytes((Path(context['roots']['SOURCE'])/'browser/host'/name).read_bytes())
    verify_input_lock(context['build_input_lock'],context['roots'])
    actual=inventory_asset_tree(out/'assets',context['expected_assets'])
    comparison=compare_asset_manifests(context['expected_assets'],actual)
    return {'schema':'panel-build-receipt-v1','complete':True,'compile_count':1,'wall_seconds':time.monotonic()-started,'asset_manifest':actual,'comparison':comparison,'commands':journal,'browser_launches':0,'operator_calls':0}


def inventory_asset_tree(directory, expected):
    """Whole-tree hashing for every launch boundary, without invoking a tool."""
    from pathlib import Path
    validate_asset_manifest(expected)
    directory=Path(directory)
    files=[]
    for path in directory.rglob('*'):
        _require(not path.is_symlink(),'asset_symlink')
        if path.is_file(): files.append(path.relative_to(directory).as_posix())
    _require(sorted(files) == expected['logical_names'],'asset_names_missing_or_extra')
    result=json.loads(json.dumps(expected))
    for row in result['assets']:
        path=directory/row['name']; row['bytes']=path.stat().st_size
        with path.open('rb') as stream:
            h=hashlib.sha256()
            for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
        row['sha256']=h.hexdigest()
    compare_asset_manifests(expected,result)
    return result


BROWSER_URL='https://cdn.playwright.dev/builds/cft/153.0.8010.12/linux64/chrome-headless-shell-linux64.zip'
BROWSER_LIMITS={'wall_seconds':180,'socket_timeout_seconds':15,'maximum_compressed_bytes':268435456,'maximum_total_uncompressed_bytes':1073741824,'maximum_single_member_bytes':536870912,'maximum_zip_entries':10000,'maximum_scalar_receipt_bytes':65536,'read_chunk_bytes':1048576}


def validate_browser_policy(policy):
    _require(type(policy) is dict and set(policy) == {'schema','chrome_product','v8_version','platform','source_url','allowed_redirect_urls','method','helper_sha256','source_metadata_sha256','playwright_core_bundle_sha256','bootstrap_helper_lock_sha256','executable_name','limits','installation_scope','identity_scope'},'browser_policy_fields')
    _require(policy['schema'] == 'panel-browser-acquisition-policy-v1' and policy['chrome_product'] == 'HeadlessChrome/153.0.8010.12' and policy['v8_version'] == '15.3.76.4' and policy['platform'] == 'linux64','browser_policy_version_platform')
    _require(policy['source_url'] == BROWSER_URL and policy['method'] == 'HEAD_THEN_ONE_GET','browser_policy_official_source_method')
    # Only an explicitly pinned same-version/platform official final URL can be
    # admitted. The shipped policy approves no redirects; there is no fallback.
    approved_final='https://storage.googleapis.com/chrome-for-testing-public/153.0.8010.12/linux64/chrome-headless-shell-linux64.zip'
    _require(policy['allowed_redirect_urls'] in ([],[approved_final]),'browser_redirect_policy_changed')
    for key in ('helper_sha256','source_metadata_sha256','playwright_core_bundle_sha256','bootstrap_helper_lock_sha256'):
        _require(type(policy[key]) is str and SHA.fullmatch(policy[key]),'browser_policy_hash')
    _require(policy['executable_name'] == 'chrome-headless-shell-linux64/chrome-headless-shell','browser_executable_name')
    _require(canonical_sha256(policy['limits']) == canonical_sha256(BROWSER_LIMITS),'browser_acquisition_limits')
    _require(policy['installation_scope'] == 'complete_official_distribution_regular_files_no_symlinks' and policy['identity_scope'] == 'acquired_once_and_pinned_within_ci_job','browser_identity_scope')
    return canonical_sha256(policy)


def validate_browser_installation_manifest(manifest):
    _require(type(manifest) is dict and set(manifest) == {'schema','executable_name','files'},'browser_installation_fields')
    _require(manifest['schema'] == 'panel-browser-installation-v1','browser_installation_schema')
    rows=manifest['files'];_require(type(rows) is list and 0 < len(rows) <= 10000,'browser_installation_count')
    names=[];total=0
    for row in rows:
        _require(type(row) is dict and set(row) == {'name','bytes','sha256','mode'},'browser_installation_row')
        name=row['name'];_require(type(name) is str and name and len(name)<=1024,'browser_installation_name')
        path=PurePosixPath(name)
        _require(not path.is_absolute() and '..' not in path.parts and path.as_posix() == name and '\\' not in name and '\x00' not in name,'browser_installation_unsafe_name')
        _require(type(row['bytes']) is int and 0 <= row['bytes'] <= 536870912,'browser_installation_bytes')
        _require(type(row['sha256']) is str and SHA.fullmatch(row['sha256']),'browser_installation_hash')
        _require(type(row['mode']) is int and row['mode'] in (0o644,0o755),'browser_installation_mode')
        names.append(name);total+=row['bytes']
    _require(names == sorted(set(names)) and total <= 1073741824,'browser_installation_roster')
    _require(manifest['executable_name'] in names,'browser_executable_missing')
    executable=next(row for row in rows if row['name']==manifest['executable_name'])
    _require(executable['bytes'] > 0 and executable['mode'] == 0o755,'browser_executable_not_executable')
    return {'chrome_support_tree_sha256':canonical_sha256(manifest),'chrome_executable_sha256':executable['sha256']}


def validate_browser_provisioning_receipt(receipt, policy):
    policy_hash=validate_browser_policy(policy)
    _require(type(receipt) is dict and set(receipt) == {'schema','status','identity_scope','source_policy_sha256','helper_sha256','acquisitions','head_requests','get_requests','redirects','head','get','archive','installation','installation_sha256','executable_format','browser_launches','operator_calls','first_failure'},'browser_provision_receipt_fields')
    _require(receipt['schema'] == 'panel-browser-provisioning-receipt-v1' and receipt['status'] == 'complete' and receipt['identity_scope'] == policy['identity_scope'],'browser_provision_status')
    _require(receipt['source_policy_sha256'] == policy_hash and receipt['helper_sha256'] == policy['helper_sha256'],'browser_provision_source_drift')
    for key in ('acquisitions','head_requests','get_requests'):_require(type(receipt[key]) is int and receipt[key] == 1,'browser_acquisition_retry')
    for key in ('browser_launches','operator_calls'):_require(type(receipt[key]) is int and receipt[key] == 0,'browser_provision_execution')
    _require(receipt['first_failure'] is None and type(receipt['redirects']) is list,'browser_provision_failure_or_redirect')
    _require(all(type(row) is dict and set(row) == {'from_url','to_url','status','method'} and row['method'] in ('HEAD','GET') for row in receipt['redirects']),'browser_redirect_receipt')
    for method,metadata in (('HEAD',receipt['head']),('GET',receipt['get'])):
        previous=policy['source_url'];visited={previous};chain=[row for row in receipt['redirects'] if row['method']==method]
        _require(len(chain) <= len(policy['allowed_redirect_urls']),'browser_redirect_hop_count')
        for row in chain:
            _require(row['from_url'] == previous and row['to_url'] in policy['allowed_redirect_urls'] and row['to_url'] not in visited and type(row['status']) is int and row['status'] in (301,302,303,307,308),'browser_redirect_chain')
            previous=row['to_url'];visited.add(previous)
        _require(type(metadata) is dict and metadata.get('url') == previous,'browser_redirect_final_source')
    _require(canonical_sha256(receipt['head']) == canonical_sha256(receipt['get']),'browser_head_get_changed')
    headers=receipt['head']
    _require(type(headers) is dict and set(headers) == {'url','status','content_length','content_type','etag'},'browser_transport_fields')
    _require(headers['url'] in [policy['source_url'],*policy['allowed_redirect_urls']] and type(headers['status']) is int and headers['status'] == 200,'browser_transport_source_status')
    _require(type(headers['content_length']) is int and 0 < headers['content_length'] <= policy['limits']['maximum_compressed_bytes'],'browser_transport_size')
    _require(headers['content_type'] in ('application/zip','application/x-zip-compressed','application/octet-stream') and type(headers['etag']) is str and 0 < len(headers['etag']) <= 512,'browser_transport_metadata')
    archive=receipt['archive'];_require(type(archive) is dict and set(archive) == {'name','bytes','sha256'},'browser_archive_fields')
    _require(archive['name'] == 'browser-distribution.zip' and type(archive['bytes']) is int and archive['bytes'] == headers['content_length'] and type(archive['sha256']) is str and SHA.fullmatch(archive['sha256']),'browser_archive_identity')
    _require(canonical_sha256(receipt['executable_format']) == canonical_sha256({'format':'ELF','bits':64,'endianness':'little','machine':62}),'browser_executable_format')
    installed=validate_browser_installation_manifest(receipt['installation'])
    _require(receipt['installation']['executable_name'] == policy['executable_name'] and installed['chrome_support_tree_sha256'] == receipt['installation_sha256'],'browser_installation_binding')
    _require(len(json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False).encode()) <= policy['limits']['maximum_scalar_receipt_bytes'],'browser_receipt_size')
    return {'chrome_distribution_sha256':archive['sha256'],**installed,'browser_installation_receipt_sha256':canonical_sha256(receipt)}


def verify_browser_installation(directory, manifest):
    """Rehash the complete retained official distribution; no OS-tree equality."""
    from pathlib import Path
    import stat
    expected=validate_browser_installation_manifest(manifest)
    root=Path(directory);_require(root.is_absolute() and root.is_dir() and not root.is_symlink(),'browser_installation_root')
    names=[]
    for path in root.rglob('*'):
        _require(not path.is_symlink(),'browser_installation_symlink')
        if path.is_file():names.append(path.relative_to(root).as_posix())
        else:_require(path.is_dir(),'browser_installation_special_file')
    _require(sorted(names) == [row['name'] for row in manifest['files']],'browser_installation_missing_or_extra')
    for row in manifest['files']:
        path=root/row['name'];observed=path.stat()
        _require(observed.st_size == row['bytes'] and stat.S_IMODE(observed.st_mode) == row['mode'],'browser_installation_size_mode_drift')
        with path.open('rb') as stream:
            checksum=hashlib.sha256()
            for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
        _require(checksum.hexdigest() == row['sha256'],'browser_installation_byte_drift')
    return expected


def verify_retained_browser(receipt,policy,archive_path,installation_root):
    from pathlib import Path
    identity=validate_browser_provisioning_receipt(receipt,policy)
    path=Path(archive_path);_require(path.is_file() and not path.is_symlink(),'browser_retained_archive_missing')
    _require(path.stat().st_size == receipt['archive']['bytes'],'browser_retained_archive_size')
    with path.open('rb') as stream:
        checksum=hashlib.sha256()
        for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
    _require(checksum.hexdigest() == identity['chrome_distribution_sha256'],'browser_retained_archive_changed')
    actual=verify_browser_installation(installation_root,receipt['installation'])
    with (Path(installation_root)/receipt['installation']['executable_name']).open('rb') as stream:header=stream.read(64)
    _require(len(header) == 64 and header[:6] == b'\x7fELF\x02\x01' and header[18:20] == b'\x3e\x00','browser_retained_executable_format')
    _require(all(actual[key] == identity[key] for key in actual),'browser_installation_identity_changed')
    return identity


def verify_bootstrap_sources(lock, source_root, expected_sha256):
    """Read inert bootstrap source bytes before any optional helper import."""
    from pathlib import Path
    source_root=Path(source_root).resolve()
    lock_path=source_root/'launch/bootstrap-helper-lock.json';raw=lock_path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == expected_sha256 and json.loads(raw) == lock,'bootstrap_helper_lock_binding')
    _require(lock.get('schema') == 'panel-bootstrap-helper-source-lock-v1','bootstrap_helper_lock_schema')
    for row in lock['donor_source_files']:
        target=source_root/row['packaged_reference']
        _require(target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(source_root),'bootstrap_reference_missing')
        _require(target.stat().st_size == row['bytes'] and hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256'],'bootstrap_reference_changed')
    return {'status':'inert_bootstrap_source_identity','lock_sha256':expected_sha256}


def verify_runtime_helpers(lock, package_roots, *, ownership_only=False):
    """Complete pinned package bytes; standalone typing_extensions is explicit."""
    from pathlib import Path
    packages=lock['runtime_helper_packages']
    _require(set(package_roots) == set(packages) == {'playwright','pyee','greenlet','psutil','typing_extensions'},'runtime_helper_roster')
    selected=('psutil',) if ownership_only else tuple(sorted(packages))
    for name in selected:
        package=packages[name];root=Path(package_roots[name]);single=name=='typing_extensions'
        _require(root.is_absolute() and (root.is_file() if single else root.is_dir()) and not root.is_symlink(),'runtime_helper_root')
        actual=[root.name] if single else []
        if not single:
            for path in root.rglob('*'):
                _require(not path.is_symlink() and '__pycache__' not in path.parts and path.suffix != '.pyc','runtime_helper_cache_or_redirect')
                if path.is_file():actual.append(path.relative_to(root).as_posix())
        rows=package['package_files'];_require(sorted(actual)==sorted(row['name'] for row in rows),'runtime_helper_missing_or_extra')
        for row in rows:
            path=root if single else root/row['name']
            _require(path.stat().st_size==row['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256'],'runtime_helper_byte_drift')
    return {'status':'complete_helper_byte_identity','packages':list(selected)}


def verify_bootstrap_helpers(lock,source_root,package_roots,expected_sha256):
    verify_bootstrap_sources(lock,source_root,expected_sha256)
    return verify_runtime_helpers(lock,package_roots)


def verify_helper_import_routes(package_roots, *, ownership_only=False):
    import importlib.machinery
    import sys
    from pathlib import Path
    names=('psutil',) if ownership_only else tuple(sorted(package_roots))
    for name in names:
        expected=Path(package_roots[name]).resolve();single=name=='typing_extensions'
        expected_origin=expected if single else expected/'__init__.py'
        if name in sys.modules:
            origin=getattr(sys.modules[name],'__file__',None)
            _require(origin is not None and Path(origin).resolve()==expected_origin,'runtime_helper_already_imported_elsewhere')
        spec=importlib.machinery.PathFinder.find_spec(name,sys.path)
        _require(spec is not None and spec.origin is not None and Path(spec.origin).resolve()==expected_origin,'runtime_helper_import_shadowed')
        _require(list(spec.submodule_search_locations or ())==([] if single else [str(expected)]),'runtime_helper_namespace_substitution')
    return {'status':'verified_import_routes','packages':list(names)}


def configure_verified_helper_imports(lock,package_roots,*,ownership_only=False):
    """Prepend only byte-verified isolated parents; reject already-loaded shadows."""
    verify_runtime_helpers(lock,package_roots,ownership_only=ownership_only)
    import sys
    from pathlib import Path
    roots={name:Path(value).resolve() for name,value in package_roots.items()}
    selected=('psutil',) if ownership_only else tuple(sorted(roots))
    parents=[]
    for name in selected:
        root=roots[name];expected=root if name=='typing_extensions' else root/'__init__.py'
        for loaded,module in tuple(sys.modules.items()):
            if loaded==name or loaded.startswith(name+'.'):
                origin=getattr(module,'__file__',None)
                _require(origin is not None,'runtime_helper_loaded_origin_unknown')
                actual=Path(origin).resolve()
                _require(actual==expected if loaded==name else actual.is_relative_to(root),'runtime_helper_already_imported_elsewhere')
        parent=root.parent;allowed={p.name for p in roots.values() if p.parent==parent}
        _require({p.name for p in parent.iterdir()} <= allowed,'runtime_helper_parent_not_isolated')
        if parent not in parents:parents.append(parent)
    # An unimported ambient installation is irrelevant. No bytes from it execute:
    # source-verified owned roots are deliberately installed first, then checked.
    for parent in reversed(parents):
        value=str(parent)
        sys.path[:]=[item for item in sys.path if item!=value]
        sys.path.insert(0,value)
    return verify_helper_import_routes(package_roots,ownership_only=ownership_only)
