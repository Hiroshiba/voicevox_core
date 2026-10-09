#!/usr/bin/env python3
"""Create a patched COPY of the pinned research harness; never edit it in place."""
import argparse,ast,hashlib,pathlib,json
BASE_SHA='d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe'
p=argparse.ArgumentParser();p.add_argument('source',type=pathlib.Path);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--helper',type=pathlib.Path,default=pathlib.Path(__file__).with_name('core_dispatch_check.js'));a=p.parse_args()
assert a.source.resolve()!=a.output.resolve(),'Refuse in-place edits'
s=a.source.read_text();assert hashlib.sha256(a.source.read_bytes()).hexdigest()==BASE_SHA,'Unexpected research harness revision'
helper=a.helper.read_text();tree=ast.parse(s);assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='BROWSER_WORKER' for t in n.targets));worker=ast.literal_eval(assignment.value)
def once(text,needle,replacement):
 assert text.count(needle)==1,needle
 return text.replace(needle,replacement)
worker=once(worker,"  }else if(data.command==='synthesize'){","""  }else if(data.command==='dispatch'){
   if(!ready)throw new Error('CORE must be initialized before dispatch proof');
   const dispatch=captureActualCoreDispatch(Module,wasmStdout,data.expected_dispatch===undefined?'auto':data.expected_dispatch);
   postMessage({dispatch});
  }else if(data.command==='synthesize'){""")
worker=helper+'\n'+worker;assert '"""' not in worker
lines=s.splitlines(keepends=True);s=''.join(lines[:assignment.lineno-1])+'BROWSER_WORKER = r"""'+worker+'"""\n'+''.join(lines[assignment.end_lineno:])
s=once(s,"    entries = manifest['variants']\n","""    entries = manifest['variants']
    runtime_dispatch_matrix = any('dispatch_expected' in entry for entry in entries)
    if runtime_dispatch_matrix:
        if manifest.get('require_exact_fp32_pcm_to_reference') is not True:
            raise ValueError('Runtime manifest must require exact FP32/PCM equality to untouched XNN')
        if not entries or entries[0].get('key') not in ('original', 'untouched') or entries[0].get('provider') != 'XNNPACK' or entries[0].get('dispatch_expected') != 'auto' or entries[0].get('build_identity', {}).get('runtime') != '407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd':
            raise ValueError('First runtime condition must be pinned untouched XNN')
        if any(entry.get('dispatch_expected') not in ('auto', 'splat', 'loadsplat') for entry in entries):
            raise ValueError('Every runtime condition requires a dispatcher expectation')
""")
s=once(s,"                        runners[mode.key] = runner\n","""                        runners[mode.key] = runner
                        if 'dispatch_expected' in entry:
                            proof = runner.page.evaluate('data => request(data)', {'command': 'dispatch', 'expected_dispatch': entry['dispatch_expected']})['dispatch']
                            if proof.get('verified'):
                                for prior_key, prior in environment.items():
                                    if prior_key.endswith('_dispatch'):
                                        if prior['worker_hardware_concurrency'] != proof['worker_hardware_concurrency']:
                                            proof.update(verified=False, error_type='Error', error_message='CORE Worker hardwareConcurrency changed across variants')
                                        elif prior['expected_dispatch'] == proof['expected_dispatch'] == 'auto' and prior['actual_dispatch'] != proof['actual_dispatch']:
                                            proof.update(verified=False, error_type='Error', error_message='Untouched/roundtrip auto dispatcher differs')
                            environment[mode.key + '_dispatch'] = proof
                            atomic_write(args.output.with_suffix('.dispatch.json'), json.dumps({key: value for key, value in environment.items() if key.endswith('_dispatch')}, indent=2).encode())
                            if not proof.get('verified'):
                                raise RuntimeError('Actual CORE Worker dispatch failed; bounded checkpoint saved')
""")
s=once(s,"                    spectrograms = output_checks.pop('spectrograms')\n","""                    spectrograms = output_checks.pop('spectrograms')
                    output_checks['exact_reference_required'] = runtime_dispatch_matrix
                    output_checks['pre_timing_gate_passed'] = False
                    atomic_write(args.output.with_suffix('.output-checks.json'), json.dumps(output_checks, indent=2).encode())
                    if runtime_dispatch_matrix and (not output_checks['reference_deterministic'] or any(not check['finite'] or not check['fp32_exact'] or not check['pcm_exact'] for check in output_checks['modes'].values())):
                        raise RuntimeError('Candidate differs from untouched XNN; numeric checks saved before timing')
""")
s=once(s,"                        output_checks['per_mode_repeat'][key] = repeated\n","""                        output_checks['per_mode_repeat'][key] = repeated
                        atomic_write(args.output.with_suffix('.output-checks.json'), json.dumps(output_checks, indent=2).encode())
""")
s=once(s,"                    result = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),\n","""                    output_checks['pre_timing_gate_passed'] = True
                    atomic_write(args.output.with_suffix('.output-checks.json'), json.dumps(output_checks, indent=2).encode())
                    result = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
""")
s+='\n# Actual CORE Worker dispatch helper SHA256: '+hashlib.sha256(a.helper.read_bytes()).hexdigest()+'\n'
ast.parse(s);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(s)
print(json.dumps({'source_sha256':BASE_SHA,'helper_sha256':hashlib.sha256(a.helper.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),'output':str(a.output),'actual_core_worker_check':True,'exact_reference_gate':True}))
