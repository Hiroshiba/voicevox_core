#!/usr/bin/env python3
"""Seal/package/test source in-process under invocation denial; no payload runs."""
from __future__ import annotations
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location('panel_finalize_'+name,ROOT/'tools'/f'{name}.py')
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def main():
    attempts=[]
    def audit(event,_):
        if event.startswith(('subprocess.','socket.','ctypes.dlopen','ctypes.dlsym','os.exec',
                             'os.spawn','os.posix_spawn','os.fork','os.system','pty.spawn')):
            attempts.append(event); raise RuntimeError('source finalization invocation denied '+event)
    sys.addaudithook(audit); sys.dont_write_bytecode=True
    evidence=ROOT/'evidence'; evidence.mkdir(exist_ok=True)
    plan_sha=hashlib.sha256((ROOT/'protocol/sealed-plan-revision3.md').read_bytes()).hexdigest()
    if plan_sha!='07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702':
        raise ValueError('sealed plan changed')
    inv=load('invocation_inventory').inventory()
    (evidence/'invocation-inventory.json').write_text(json.dumps(inv,sort_keys=True,indent=2)+'\n')
    history=load('check_history').check()
    (evidence/'history-preservation.json').write_text(json.dumps(history,sort_keys=True,indent=2)+'\n')
    lineage=load('check_lineage').check()
    (evidence/'parent-lineage-preservation.json').write_text(json.dumps(lineage,sort_keys=True,indent=2)+'\n')
    seal=load('freeze_sources').seal()
    packer=load('package_launcher')
    first=packer.package(); second=packer.package()
    if first != second: raise ValueError('standalone packaging is not byte reproducible')
    launcher=ROOT/'dist/chrome_panel_order_benchmark.py'
    namespace=runpy.run_path(str(launcher),run_name='panel_source_finalizer_test')
    entries=namespace['verified_files']()
    if entries != packer.inventory(): raise ValueError('full standalone inventory mismatch')
    old_cwd=Path.cwd(); old_environment=dict(os.environ); blocked=0
    with tempfile.TemporaryDirectory(prefix='panel-source-only-') as tmp:
        wrong=Path(tmp)/'wrong-workspace'; wrong.mkdir()
        try:
            os.chdir(wrong)
            for inherited in (False,True):
                os.environ.clear()
                if inherited:
                    os.environ.update(GITHUB_ACTIONS='true',GITHUB_WORKSPACE=str(wrong),
                                      RUNNER_TEMP=str(wrong),CI='true',PANEL_GO='true')
                for argv in ([],['--actions'],['--compile'],['--qualify'],['--timing'],
                             ['--run'],['--request','made-up.json']):
                    try: namespace['main'](argv)
                    except SystemExit as exc:
                        if 'EXECUTION_ADMISSION_REQUIRED' not in str(exc): raise
                    else: raise ValueError('full standalone runtime mode did not block')
                    blocked+=1
            output=Path(tmp)/'exact-extraction'
            namespace['extract_only'](output)
            for name,data in entries.items():
                if (output/name).read_bytes()!=data: raise ValueError('extraction mismatch')
            samples=[dict(root=r,dilation=d,arm=a,duration_ms=10.0+r/1000)
                     for r in range(1,11) for d in (1,3,5) for a in ('R','A','B','Q','P')]
            document=dict(evidence='synthetic_source_test',actual_actions_run=False,
                          statistics=load('statistics').analyze_primary_samples(samples))
            synthetic=evidence/'synthetic-source-test'; synthetic.mkdir(exist_ok=True)
            source=synthetic/'input.json'
            source.write_text(json.dumps(document,sort_keys=True,indent=2,allow_nan=False)+'\n')
            rendered=Path(tmp)/'synthetic.html'
            with contextlib.redirect_stdout(io.StringIO()):
                namespace['main'](['--render-synthetic',str(source),str(rendered)])
            expected=load('render_report').render_synthetic(document)
            if rendered.read_text()!=expected: raise ValueError('standalone HTML changed')
            (synthetic/'report.html').write_text(expected)
        finally:
            os.chdir(old_cwd); os.environ.clear(); os.environ.update(old_environment)
    load('freeze_sources').verify()
    receipt=dict(schema='chrome-panel-final-source-package-audit-v1',
                 source_only_preparation=True,execution_capable_requires_root_GO=True,performance_evidence=False,
                 sealed_plan_sha256=plan_sha,
                 source_tree_canonical_sha256=seal['source_tree_canonical_sha256'],
                 manifest_sha256=hashlib.sha256((ROOT/'MANIFEST.json').read_bytes()).hexdigest(),
                 source_files=seal['file_count'],source_bytes=seal['source_bytes'],
                 launcher_sha256=first['launcher_sha256'],launcher_bytes=first['launcher_bytes'],
                 source_package_sha256=first['payload_sha256'],
                 deterministic_double_package=True,exact_full_inventory_and_extraction=True,
                 wrong_workspace_runtime_blocks=blocked,ordinary_and_inherited_Actions=True,
                 synthetic_html_sha256=hashlib.sha256(expected.encode()).hexdigest(),
                 invocation_inventory_sha256=hashlib.sha256((evidence/'invocation-inventory.json').read_bytes()).hexdigest(),
                 complete_new_source_patch_sha256=hashlib.sha256((evidence/'new-source-tree.patch').read_bytes()).hexdigest(),
                 donor_diff_sha256=hashlib.sha256((evidence/'source-diffs.patch').read_bytes()).hexdigest(),
                 activation_delta_sha256=hashlib.sha256((evidence/'activation-delta.patch').read_bytes()).hexdigest(),
                 frozen_parent90_and_compile189_unchanged=lineage['all_bytes_unchanged'],
                 historical_69_source_files_unchanged=True,
                 process_network_native_attempts=attempts,passed=not attempts)
    (evidence/'final-source-package-audit.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    print(json.dumps(receipt,sort_keys=True))

if __name__=='__main__': main()
