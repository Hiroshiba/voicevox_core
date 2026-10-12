"""Explicit admission bootstrap for a closed roster of isolated Python children."""
import sys
if not sys.flags.isolated or not sys.dont_write_bytecode:raise SystemExit('isolated_no_bytecode_required')
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
# Only the sibling source root is introduced; admission rehashes it before any
# optional helper/native/process/network import. -I excludes ambient cwd paths.
if not sys.flags.isolated or not sys.dont_write_bytecode:raise SystemExit('isolated_no_bytecode_required')
for _name,_module in tuple(sys.modules.items()):
    if _name.split('.')[0] in ('tools','launch','browser'):
        _origin=getattr(_module,'__file__',None);_paths=list(getattr(_module,'__path__',()))
        if _origin is not None:
            if not Path(_origin).resolve().is_relative_to(ROOT):raise RuntimeError('source_package_already_shadowed')
        elif not _paths or any(not Path(path).resolve().is_relative_to(ROOT) for path in _paths):
            raise RuntimeError('source_package_already_shadowed')
sys.path.insert(0,str(ROOT))
from launch.source_only import child_admission,CHILD_TARGETS,consume_child_lease

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--admission',required=True);p.add_argument('--lease',required=True);p.add_argument('--script',required=True)
    p.add_argument('args',nargs=argparse.REMAINDER)
    a=p.parse_args(argv);state=child_admission(a.admission)
    target=Path(a.script)
    if not target.is_absolute() or not target.is_relative_to(ROOT) or target.relative_to(ROOT).as_posix() not in CHILD_TARGETS:raise ValueError('child_target_unapproved')
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    consume_child_lease(state,a.lease,target,args)
    from tools.program import derive_context
    from tools.build_driver import configure_verified_helper_imports
    context=derive_context(ROOT,Path(state['work_root']),sys.executable)
    ownership_only=not all(Path(path).exists() for path in context['runtime_helper_package_roots'].values())
    configure_verified_helper_imports(context['bootstrap_helper_lock'],context['runtime_helper_package_roots'],ownership_only=ownership_only)
    import runpy
    args=a.args[1:] if a.args[:1]==['--'] else a.args
    sys.argv=[str(target),*args]
    runpy.run_path(str(target),run_name='__main__')

if __name__=='__main__':main()
