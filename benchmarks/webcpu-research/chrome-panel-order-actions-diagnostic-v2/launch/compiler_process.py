"""Pinned compiler command and limits; exact admitted child lease required."""
import sys
if __name__ == '__main__' and (not sys.flags.isolated or not sys.dont_write_bytecode):
    raise SystemExit('isolated_no_bytecode_required')


import sys
from pathlib import Path
_SOURCE_ROOT=Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:sys.path.insert(0,str(_SOURCE_ROOT))
from launch.source_only import require_execution_admission


def main(argv=None):
    admission=require_execution_admission('compiler_process.main')
    import argparse
    import json
    import os
    import resource
    parser=argparse.ArgumentParser();parser.add_argument('--spec',required=True);a=parser.parse_args(argv)
    spec=json.loads(Path(a.spec).read_text())
    if set(spec) != {'id','argv','cwd','env'}:raise ValueError('compiler_spec_fields')
    if admission['phase']!='compile':raise ValueError('compiler_phase_scope')
    from tools.program import derive_context
    from tools.build_driver import render_tokens,validate_recipe
    context=derive_context(_SOURCE_ROOT,Path(admission['work_root']),sys.executable)
    recipe=context['build_recipe'];validate_recipe(recipe)
    commands=[row for row in recipe['commands'] if row['id']==spec['id'] and row['kind']!='archive_prepare']
    if len(commands)!=1:raise ValueError('compiler_command_not_admitted')
    command=render_tokens(commands[0],context['roots']);environment=render_tokens(recipe['environment'],context['roots'])
    if spec!={'id':command['id'],'argv':command['argv'],'cwd':command['cwd'],'env':environment}:raise ValueError('compiler_command_drift')
    expected=Path(context['roots']['OUT'])/'logs'/spec['id']/'command-spec.private.json'
    if Path(a.spec)!=expected or expected.is_symlink():raise ValueError('compiler_spec_location')
    resource.setrlimit(resource.RLIMIT_CPU,(480,480));resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    if spec['id'] not in ('link_R','link_A','link_B','link_Q','link_P'):
        resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,4*1024**3))
    # Exact five final Emscripten JS links alone retain the historical large
    # compiler-helper virtual-address reservation exception. RSS/CPU/wall/disk,
    # fixed helper concurrency and no-workload restrictions remain unchanged.
    os.chdir(spec['cwd'])
    os.execve(spec['argv'][0],spec['argv'],spec['env'])


if __name__ == '__main__':
    main()
