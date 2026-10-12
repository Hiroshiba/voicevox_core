"""Pure mocked fresh-run plumbing, never a job, child process or payload.

Real directory/argv/lease/phase/transition code runs on scalar test data. All
ownership launches, acquisition, host collection, tool execution and asset data
production are replaced by explicitly synthetic in-process fixtures. No root
admission is opened; the process-local state below exists only under mock.patch.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from launch import source_only as admission
from tools import program, build_driver as build
from tools.transition import digest
from test_stage_gates import expected, browser_receipt, synthetic_receipt

FIELDS = ('GITHUB_ACTIONS', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'GITHUB_SHA', 'RUNNER_TEMP')


class DryIntegrationTests(unittest.TestCase):
    def fixture(self, parent):
        work = parent / 'panel-order-screen-attempt'
        work.mkdir()
        (work / 'child-leases').mkdir()
        descriptor = work / 'admission.private.json'
        descriptor.write_text('{"evidence":"synthetic_source_test"}')
        state = {'source_root': str(ROOT), 'work_root': str(work), 'phase': 'provision',
                 'descriptor_path': str(descriptor),
                 'descriptor_sha256': hashlib.sha256(descriptor.read_bytes()).hexdigest()}
        environment = dict(zip(FIELDS, ('true', 'synthetic', '1', 'job', 'b' * 40, str(parent))))
        with mock.patch.dict(os.environ, environment):
            context = program.derive_context(ROOT, work, Path(sys.executable).absolute())
        return work, state, environment, context

    def consume(self, command, state):
        self.assertEqual(command[:3], [sys.executable, '-B', '-I'])
        self.assertEqual(command[3], str(ROOT / 'launch/child_entry.py'))
        self.assertEqual(command[4:6], ['--admission', state['descriptor_path']])
        self.assertEqual(command[6], '--lease')
        self.assertEqual(command[8], '--script')
        self.assertEqual(command[10], '--')
        target, arguments = Path(command[9]), command[11:]
        lease = json.loads(Path(command[7]).read_text())
        self.assertEqual(lease['script'], str(target))
        self.assertEqual(lease['args'], arguments)
        self.assertEqual(lease['phase'], state['phase'])
        admission.consume_child_lease(state, command[7], target, arguments)
        with self.assertRaises(FileExistsError):
            admission.consume_child_lease(state, command[7], target, arguments)
        return target, arguments

    def chain(self, parent, qualification_failure=False):
        from launch import bootstrap_fetch
        work, state, environment, context = self.fixture(parent)
        exp = expected()
        exp['bindings']['job_id'] = exp['identity']['job_id'] = 'synthetic:1:job'
        exp['owned_temp_roots'] = {s: str(work / s / 'tmp') for s in ('qualification', 'timing')}
        template = {'bindings': exp['bindings'], 'asset_manifest': exp['asset_manifest'],
                    'browser_acquisition_policy': json.loads((ROOT/'launch/browser-acquisition-policy.json').read_text())}
        request = {'source_release': 'a' * 40,
                   'provisioning_contract_sha256': digest(context['provisioning_plan']),
                   'build_input_lock_sha256': digest(context['bootstrap_inputs'])}
        template['bindings']['build_recipe_sha256'] = digest(context['build_recipe'])
        request_path = parent / 'synthetic-request.json'
        request_path.write_text(json.dumps(request))
        phases = []
        bootstrap_calls = []

        def bootstrap(ctx, deadline):
            bootstrap_calls.append(deadline)
            # Exercise the fresh path needed by the initial helper bootstrap;
            # no download, extraction, import or actual helper bytes occur.
            destination = Path(ctx['provision_root']) / 'deps/python'
            destination.mkdir(parents=True)
            return {'evidence': 'synthetic_source_test', 'complete': True}

        def supervised(command, cwd, env, deadline, output_root, limits, journal):
            target, args = self.consume(command, state)
            self.assertEqual(target, ROOT / 'launch/phase_process.py')
            values = dict(zip(args[::2], args[1::2]))
            stage = values['--stage']; phases.append(stage)
            self.assertEqual(Path(cwd), work / stage)
            self.assertEqual(Path(output_root), work / stage)
            self.assertTrue(Path(cwd).is_dir())
            self.assertEqual({k: env[k] for k in FIELDS}, environment)
            self.assertEqual(limits['stage'], stage)
            supplied = json.loads(Path(values['--context']).read_text())
            self.assertEqual(Path(values['--ready']).parent, work / stage)
            self.assertEqual(Path(values['--start-gate']).parent, work / stage)
            if stage == 'provision':
                self.assertEqual(len(bootstrap_calls), 1)
                self.assertTrue((Path(supplied['provision_root']) / 'deps/python').is_dir())
                receipt = {'complete': True, 'operator_calls': 0, 'browser_launches': 0,
                           'portable_build_lock': {}, 'baseline_inventory': [],
                           'browser_provisioning_receipt': browser_receipt(context['browser_acquisition_policy'])}
            elif stage == 'compile':
                self.assertEqual(supplied['build_input_lock'], {})
                self.assertEqual(supplied['baseline_members'], [])
                receipt = {'complete': True, 'compile_count': 1, 'asset_manifest': exp['asset_manifest']}
            else:
                self.assertEqual(supplied['platform_inputs']['owned_temp_root'], exp['owned_temp_roots'][stage])
                receipt = synthetic_receipt(stage, exp, supplied.get('fixture_identities'))
                if stage == 'qualification' and qualification_failure:
                    receipt['calls'][0]['evidence']['witnesses']['checked'] = 0
                if stage == 'timing':
                    self.assertEqual(len(supplied['phase_records']), 3)
                    self.assertEqual(len(supplied['fixture_identities']), 33)
                    self.assertEqual(phases, ['provision', 'compile', 'qualification', 'timing'])
            Path(values['--output']).write_text(json.dumps(receipt))
            outer = synthetic_receipt('qualification', exp)['resources']['outer_supervision']
            note = {'acknowledged': True, 'cleanup': outer['cleanup'],
                    'startup_gate_released': True, 'samples': outer['samples'],
                    'wall_seconds': 1.0, 'tree_cpu_seconds': 1.0}
            journal.append(copy.deepcopy(note))
            return note

        patches = {
            'validate_request': mock.Mock(return_value=template),
            'verify_source_release': mock.Mock(), 'verify_bound_source_facts': mock.Mock(),
            'collect_allocated_host': mock.Mock(return_value={'job_id': 'synthetic:1:job'}),
            'optional_cache_metadata': mock.Mock(return_value={'status': 'unknown', 'gating': False, 'rows': []}),
            'observe_host_tools': mock.Mock(return_value={}), 'verify_bootstrap_sources': mock.Mock(),
            'configure_verified_helper_imports': mock.Mock(), 'verify_retained_browser': mock.Mock(),
            'bind_allocated_host': mock.Mock(return_value=exp),
            'inventory_asset_tree': mock.Mock(return_value=exp['asset_manifest']),
            'supervise_command': mock.Mock(side_effect=supervised),
        }
        with mock.patch.object(admission, '_STATE', state), mock.patch.dict(os.environ, environment), \
             mock.patch.object(bootstrap_fetch, 'bootstrap_psutil', side_effect=bootstrap), \
             mock.patch.multiple(program, **patches):
            if qualification_failure:
                with self.assertRaises(ValueError):
                    program.run(request_path, context, work, time.monotonic() - 1, {})
                self.assertEqual(phases, ['provision', 'compile', 'qualification'])
                self.assertFalse((work / 'timing').exists())
                receipt = json.loads((work / 'program-terminal.json').read_text())
                self.assertIsNotNone(receipt['first_failure'])
                exported=json.loads((work/'public-scalars/receipt.json').read_text())
                self.assertTrue(exported['projection_complete'],exported['projection_issue_locations'])
                self.assertEqual(len(exported['qualification']['calls']),330)
                self.assertIsNone(exported['timing'])
                self.assertNotIn('synthetic:1:job',json.dumps(exported))
                return
            result = program.run(request_path, context, work, time.monotonic() - 1, {})
        self.assertEqual(phases, ['provision', 'compile', 'qualification', 'timing'])
        self.assertEqual(len(bootstrap_calls), 1)
        self.assertEqual(result['status'], 'completed_pending_independent_terminal_audit')
        self.assertEqual(len(result['qualification']['calls']), 330)
        self.assertEqual(len(result['timing']['calls']), 600)
        self.assertTrue((work / 'finalization-scalar.json').is_file())
        manifest=json.loads((work/'public-scalars/manifest.json').read_text())
        exported=json.loads((work/'public-scalars/receipt.json').read_text())
        self.assertTrue(manifest['projection_complete'],exported['projection_issue_locations'])
        self.assertEqual(len(exported['qualification']['calls']),330)
        self.assertEqual(len(exported['timing']['calls']),600)
        self.assertEqual(len(exported['phase_supervision']),4)
        self.assertEqual(len(list((work / 'child-leases').glob('*.consumed'))), 4)

    def test_fresh_mocked_program_entire_conditional_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            self.chain(Path(directory))

    def test_qualification_discrepancy_stops_before_timing_directory_or_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            self.chain(Path(directory), qualification_failure=True)

    def test_all_38_compiler_wrappers_preserve_spec_environment_and_bind_full_argv(self):
        from launch import archive_recipe
        with tempfile.TemporaryDirectory() as directory:
            work, state, environment, context = self.fixture(Path(directory))
            state['phase'] = 'compile'
            context.update(build_input_lock={}, baseline_members=[], expected_assets=expected()['asset_manifest'])
            recipe = context['build_recipe']; seen = []; archives = []
            expanded = {row['id']: build.render_tokens(row, context['roots']) for row in recipe['commands']}
            exact_env = build.render_tokens(recipe['environment'], context['roots'])
            def outputs(row):
                for name in row['outputs']:
                    p = Path(name); p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text('SYNTHETIC SOURCE TEST PLACEHOLDER; NO COMPILED CODE\n')
            def archive(*args):
                archives.append(args)
                outputs(next(v for v in expanded.values() if v['kind'] == 'archive_prepare'))
            def supervised(command, cwd, env, deadline, output_root, limits, journal):
                target, argv = self.consume(command, state)
                self.assertEqual(target, ROOT / 'launch/compiler_process.py')
                self.assertEqual(argv[0], '--spec'); self.assertEqual(len(argv), 2)
                spec = json.loads(Path(argv[1]).read_text()); row = expanded[spec['id']]
                self.assertEqual(spec['env'], exact_env)
                self.assertEqual(spec['argv'], row['argv'])
                self.assertEqual(spec['cwd'], row['cwd'])
                self.assertEqual({k: env[k] for k in FIELDS}, environment)
                self.assertEqual({k: v for k, v in env.items() if k not in FIELDS}, exact_env)
                seen.append(spec['id']); outputs(row)
                journal.append({'complete': True}); return {'complete': True}
            with mock.patch.object(admission, '_STATE', state), \
                 mock.patch.object(build, 'verify_input_lock'), \
                 mock.patch.object(build, 'supervise_command', side_effect=supervised), \
                 mock.patch.object(archive_recipe, 'run', side_effect=archive), \
                 mock.patch.object(build, 'inventory_asset_tree', return_value=context['expected_assets']):
                result = build.compile_once(recipe, context, time.monotonic() + 600, [])
            self.assertEqual(seen, [r['id'] for r in recipe['commands'] if r['kind'] != 'archive_prepare'])
            self.assertEqual(len(seen), 38); self.assertEqual(len(archives), 1)
            self.assertEqual(len(list((work / 'child-leases').glob('*.consumed'))), 38)
            self.assertTrue(result['complete'])

    def test_initial_helper_creates_fully_fresh_parent_without_process_or_import(self):
        from launch import bootstrap_fetch
        with tempfile.TemporaryDirectory() as directory:
            work, state, environment, context = self.fixture(Path(directory))
            root = Path(context['provision_root'])
            self.assertFalse(root.parent.exists())
            def acquire(row, destination, deadline):
                self.assertEqual(row['id'], 'wheel-psutil')
                self.assertTrue(destination.parent.is_dir())
                destination.write_text('SYNTHETIC SOURCE TEST; NOT A WHEEL')
                return {'evidence': 'synthetic_source_test', 'get_requests': 0}
            def extract(row, archive, destination, deadline):
                self.assertTrue(archive.is_file())
                (destination / 'psutil').mkdir(parents=True)
                return {'evidence': 'synthetic_source_test', 'members': 0}
            with mock.patch.object(admission, '_STATE', state), \
                 mock.patch('shutil.disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)), \
                 mock.patch.object(bootstrap_fetch, 'acquire', side_effect=acquire), \
                 mock.patch.object(bootstrap_fetch, 'extract', side_effect=extract), \
                 mock.patch.object(bootstrap_fetch, 'verify_roster', return_value={'verified': True}):
                result = bootstrap_fetch._bootstrap_psutil_work(context, time.monotonic() + 150)
            self.assertTrue(result['complete'])
            self.assertFalse(result['production_supervised'])
            self.assertEqual(result['subprocesses'], 0)
            self.assertTrue((root / 'deps/python/psutil').is_dir())
            self.assertTrue((root / 'bootstrap-psutil-receipt.json').is_file())
