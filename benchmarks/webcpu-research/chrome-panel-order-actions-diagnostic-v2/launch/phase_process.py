"""Isolated phase child; exact one-use child admission is required."""
import sys
if __name__ == '__main__' and (not sys.flags.isolated or not sys.dont_write_bytecode):
    raise SystemExit('isolated_no_bytecode_required')


# Only isolated Python reaches the exact child admission gate.
import sys
from pathlib import Path
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path: sys.path.insert(0,str(_SOURCE_ROOT))
from launch.source_only import require_execution_admission


def main(argv=None):
    admission=require_execution_admission('phase_process.main')
    import argparse
    import json
    import resource
    import time
    from pathlib import Path
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',choices=('provision','compile','qualification','timing'),required=True)
    for name in ('context','output','ready','start-gate'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--deadline',type=float,required=True)
    a=parser.parse_args(argv)
    if a.stage!=admission['phase']:raise ValueError('phase_lease_scope')
    phase_root=Path(admission['work_root'])/a.stage
    for name,filename in (('context','context.private.json'),('output','receipt.private.json'),('ready','phase-ready.json'),('start_gate','phase-start.json')):
        if Path(getattr(a,name))!=phase_root/filename:raise ValueError('phase_owned_path')
    cpu={'provision':2700,'compile':480,'qualification':1200,'timing':2400}[a.stage]
    resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))
    # Separate processes ensure timing does not attempt to raise qualification's
    # inherited 1200-second hard limit. Set file/core limits before runtime import.
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    context=json.loads(Path(a.context).read_text())
    with Path(a.ready).open('x') as stream:json.dump({'stage':a.stage,'cpu_limit':cpu,'deadline':a.deadline},stream)
    while not Path(a.start_gate).exists():
        if time.monotonic() >= a.deadline-30:raise RuntimeError('outer_observation_start_gate_timeout')
        time.sleep(.01)
    gate=json.loads(Path(a.start_gate).read_text())
    if gate != {'stage':a.stage,'owned_tree_observed':True}:raise RuntimeError('invalid_outer_start_gate')
    journal=[]
    failed=None
    try:
        if a.stage == 'provision':
            from tools.build_driver import provision
            result=provision(context['provisioning_plan'],context,a.deadline,journal)
        elif a.stage == 'compile':
            from tools.build_driver import compile_once
            result=compile_once(context['build_recipe'],context,a.deadline,journal)
        else:
            from browser.browser import run
            context['runtime_dir']=str(Path(a.output).parent)
            result=run(a.stage,context,a.deadline)
    except BaseException as error:
        failed=error
        from tools.failure_diagnostics import phase_failure
        result=phase_failure(a.stage,error,journal)
    raw=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw) > 32*1024**2:raise RuntimeError('phase_receipt_budget')
    with Path(a.output).open('xb') as stream:stream.write(raw)
    if failed is not None:raise failed

if __name__ == '__main__':
    main()
