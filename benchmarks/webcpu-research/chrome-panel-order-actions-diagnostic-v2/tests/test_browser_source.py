"""Pure Python/source/mock tests. JavaScript is read, NEVER evaluated."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from browser.browser import (ARMS, BrowserGateRejected, BufferLiveness, LifecycleModel,
    PhaseCarry, configuration, expected_schedule, optional_cache_metadata,
    validate_browser_installation_binding, validate_calibration, validate_launch_argv, validate_phases, validate_probe_timing, validate_resource_window, validate_schedule)
from launch.source_only import SourceOnlyBlocked
import browser.browser as browser


def phases(offset=0):
    return [{'region': i, 'mod64': offset % 64, 'mod4096': offset} for i in range(7)]


class SourceInterlocks(unittest.TestCase):
    def test_runtime_functions_fail_before_arguments_are_touched(self):
        class Poison:
            def __getattribute__(self, _):
                raise AssertionError('arguments inspected before source blocker')
        for name in ('run','main','launch','flag_probe','qualify','timing','load_production_driver'):
            with self.subTest(name=name), self.assertRaises(SourceOnlyBlocked):
                function = getattr(browser, name)
                function() if name == 'load_production_driver' else function(Poison())

    def test_wrong_workspace_and_actions_cli_remain_blocked(self):
        previous = Path.cwd()
        original = {k: os.environ.get(k) for k in ('GITHUB_ACTIONS','RUNNER_ENVIRONMENT','PANEL_RUNTIME_GO')}
        try:
            os.chdir('/tmp')
            for inherited in (False, True):
                os.environ.update(GITHUB_ACTIONS=str(inherited).lower(), RUNNER_ENVIRONMENT='github-hosted', PANEL_RUNTIME_GO='yes')
                with self.assertRaisesRegex(SystemExit, 'SOURCE_ONLY_EXECUTION_BLOCK'):
                    runpy.run_path(str(ROOT/'browser/browser.py'), run_name='__main__')
        finally:
            os.chdir(previous)
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_production_donor_bytes_and_guards(self):
        manifest = json.loads((ROOT/'browser/vendor/source-bindings.json').read_text())
        self.assertEqual(len(manifest['records']), 5)
        for row in manifest['records']:
            source = (ROOT/row['reference']).read_bytes()
            guarded = (ROOT/row['guarded']).read_bytes()
            self.assertEqual(hashlib.sha256(source).hexdigest(), row['donor_sha256'])
            self.assertEqual(hashlib.sha256(guarded).hexdigest(), row['guarded_sha256'])
            tree = ast.parse(guarded)
            statements = [node for node in tree.body if not
                (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)) and not
                (isinstance(node, ast.ImportFrom) and node.module == '__future__')]
            self.assertIsInstance(statements[0], ast.ImportFrom)
            self.assertEqual(statements[0].module, 'launch.source_only')
            self.assertIn('require_execution_admission(', ast.unparse(statements[1]))
            # Parse only; never import/evaluate production process/network donors.
            recovered = guarded.decode().replace(row['only_delta'], '', 1)
            for change in reversed(row.get('integration_deltas', [])):
                recovered = recovered.replace(change['after'], change['before'], 1)
            self.assertEqual(recovered.encode(), source)

    def test_production_has_no_new_local_fallback(self):
        text = (ROOT/'browser/browser.py').read_text()
        self.assertIn('self.driver.OperatorBrowser(', text)
        self.assertIn('self.driver.observed_call(', text)
        self.assertIn('self.driver.strict_flag_acceptance_probe(', text)
        self.assertNotIn('chromium.launch(', text)
        self.assertNotIn('subprocess.Popen(', text)
        self.assertNotIn('killpg(', text)
        strict = (ROOT/'browser/vendor/reference/strict_resources.py.txt').read_text()
        owner = (ROOT/'browser/vendor/reference/process_guard.py.txt').read_text()
        self.assertIn("'children'", strict)
        self.assertIn('pidfd_open', owner)
        self.assertIn('pidfd_send_signal', owner)
        self.assertIn('become_subreaper', owner)

    def test_all_owned_python_children_use_shared_admission_wrapper(self):
        owner = ast.parse((ROOT/'tools/process_guard.py').read_text())
        cls = next(n for n in owner.body if isinstance(n, ast.ClassDef) and n.name == 'OwnedProcess')
        ctor = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        self.assertEqual(sum(isinstance(n, ast.Call) and ast.unparse(n.func) == 'child_python_command'
            for n in ast.walk(ctor)), 1)
        driver = (ROOT/'browser/vendor/operator_browser.py').read_text()
        self.assertEqual(driver.count('child_python_command(Path(__file__).resolve(),'), 2)
        self.assertNotIn("[sys.executable, str(Path(__file__).resolve()), '--owned-browser-child'", driver)
        self.assertNotIn("[sys.executable, str(Path(__file__).resolve()), '--strict-flag-acceptance-probe'", driver)

    def test_supervisor_lease_binds_complete_command_with_mocked_wrapper(self):
        # Execute only the AST's pure argv construction, never import the donor
        # or evaluate its constructor/socket/process operations.
        tree = ast.parse((ROOT/'tools/process_guard.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OwnedProcess')
        ctor = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        block = next(n for n in ctor.body if isinstance(n, ast.Try))
        statements = []
        for node in block.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == 'process' for t in node.targets):
                break
            statements.append(node)
        self.assertEqual(ast.unparse(statements[-1]), 'args = child_python_command(Path(__file__).resolve(), args)')
        self.assertEqual({ast.unparse(n.func) for node in statements for n in ast.walk(node) if isinstance(n, ast.Call)},
            {'str', 'map', 'child.fileno', 'Path', 'Path(__file__).resolve', 'child_python_command'})
        code = compile(ast.Module(body=statements, type_ignores=[]), '<pure-supervisor-argv-model>', 'exec')
        for full in (False, True):
            sealed = []
            class FakeDescriptor:
                def fileno(self): return 42
            def mock_child_python_command(script, args):
                sealed.append((str(script), list(args)))
                return ['mock-python', '-B', '-I', 'mock-child-entry', '--', *args]
            namespace = dict(Path=Path, __file__='/owned/tools/process_guard.py', child=FakeDescriptor(),
                cpu_limit=1200 if full else None, cwd='/owned/work' if full else None,
                deadline=100.0 if full else None, cleanup_deadline=130.0 if full else None,
                command=['payload', '--input', 'fixture'], child_python_command=mock_child_python_command)
            exec(code, namespace)
            expected = ['--control-fd', '42']
            if full:
                expected += ['--cpu-limit', '1200', '--cwd', '/owned/work', '--deadline', '100.0', '--cleanup-deadline', '130.0']
            expected += ['--', 'payload', '--input', 'fixture']
            self.assertEqual(sealed, [('/owned/tools/process_guard.py', expected)])
            self.assertEqual(namespace['args'], ['mock-python', '-B', '-I', 'mock-child-entry', '--', *expected])

    def test_derived_helper_pins_match_actual_activated_bytes(self):
        source = (ROOT/'browser/vendor/operator_browser.py').read_text()
        tree = ast.parse(source)
        pins = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in n.targets))
        for name, expected in pins.items():
            self.assertEqual(hashlib.sha256((ROOT/'browser/vendor'/name).read_bytes()).hexdigest(), expected)
        attribution = ast.parse((ROOT/'browser/vendor/attribution_browser.py').read_text())
        base = next(ast.literal_eval(n.value) for n in attribution.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'BASE_SHA' for t in n.targets))
        self.assertEqual(base, pins['affine_browser.py'])
        self.assertIn('DEFAULT_HELPERS = ROOT', source)
        self.assertNotIn("path=root.parent/'baseline'", (ROOT/'browser/vendor/attribution_browser.py').read_text())

    def test_legacy_donor_workloads_and_unowned_browser_stay_disabled(self):
        for name in ('operator_browser.py', 'affine_browser.py', 'attribution_browser.py'):
            tree = ast.parse((ROOT/'browser/vendor'/name).read_text())
            run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
            self.assertIsInstance(run.body[0], ast.Raise)
        tree = ast.parse((ROOT/'browser/vendor/affine_browser.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Browser')
        ctor = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        self.assertIsInstance(ctor.body[0], ast.Raise)
        for name in ('flag_acceptance_probe', '_flag_acceptance_child'):
            method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertIsInstance(method.body[0], ast.Raise)

    def test_all_adapter_methods_start_with_blocker(self):
        tree = ast.parse((ROOT/'browser/browser.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ProductionIntegration')
        for method in cls.body:
            if isinstance(method, ast.FunctionDef):
                self.assertIn('require_execution_admission(', ast.unparse(method.body[0]), method.name)

    def test_runtime_helpers_verified_before_optional_imports(self):
        tree = ast.parse((ROOT/'browser/browser.py').read_text())
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        self.assertIn('require_execution_admission(', ast.unparse(run.body[0]))
        calls = {ast.unparse(n.func): n for n in ast.walk(run) if isinstance(n, ast.Call)}
        admission = calls['configure_verified_helper_imports']
        self.assertEqual([ast.unparse(arg) for arg in admission.args],
            ["context['bootstrap_helper_lock']", "context['runtime_helper_package_roots']"])
        self.assertLess(admission.lineno, calls['ScalarJournal'].lineno)
        self.assertLess(admission.lineno, calls['_run_stage'].lineno)
        self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom)) and
            any(alias.name in ('psutil', 'playwright', 'greenlet', 'pyee') for alias in n.names)
            for n in ast.walk(run)))
        stage = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_run_stage')
        self.assertIn('require_execution_admission(', ast.unparse(stage.body[0]))
        self.assertNotIn('importlib.metadata', ast.unparse(stage))
        self.assertIn('from playwright._repo_version import version as playwright_version', ast.unparse(stage))
        self.assertIn('psutil.__version__', ast.unparse(stage))
        self.assertIn("packages['playwright']['version']", ast.unparse(run))

    def test_outer_owner_cpu_limit_is_set_before_payload(self):
        source = (ROOT/'tools/process_guard.py').read_text()
        self.assertIn('stderr=None, cpu_limit=None)',source)
        self.assertIn("args += ['--cpu-limit', str(cpu_limit)]",source)
        body = source.split('def _supervise(')[1].split('class OwnedProcess')[0]
        self.assertLess(body.index('resource.setrlimit(resource.RLIMIT_CPU'),body.index('job.child = subprocess.Popen'))
        self.assertIn('resource.getrlimit(resource.RLIMIT_CPU)',body)

    def test_all_launches_require_complete_browser_rehash_and_explicit_path(self):
        tree=ast.parse((ROOT/'browser/browser.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ProductionIntegration')
        for name,launch in (('probe','strict_flag_acceptance_probe'),('open','OperatorBrowser')):
            method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
            source=ast.unparse(method)
            self.assertLess(source.index('self.verify_installation()'),source.index('self.driver.'+launch+'('))
            self.assertIn('chrome_executable=self.chrome_executable',source)
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='verify_installation')
        self.assertIn('verify_browser_installation(directory, manifest)',ast.unparse(method))
        source=(ROOT/'browser/vendor/operator_browser.py').read_text()
        local=source.split('class _LocalOperatorBrowser:')[1].split('def _cleanup_projection')[0]
        self.assertIn('executable_path=chrome_executable',local)
        self.assertLess(local.index('file_sha(Path(chrome_executable))'),local.index('pw.chromium.launch('))
        self.assertNotIn('pw.chromium.executable_path',local)
        self.assertIn('integration.verify_installation()',(ROOT/'browser/browser.py').read_text())


class CallAndPhaseModels(unittest.TestCase):
    def setUp(self):
        self.calls = expected_schedule()

    def test_exact_calls_and_count(self):
        self.assertEqual(len(validate_schedule(self.calls)), 930)
        self.assertEqual(sum(c['retained'] for c in self.calls), 150)
        self.assertEqual(sum(c['reference'] for c in self.calls), 63)
        self.assertEqual(sum(c['comparison_words'] for c in self.calls), 6832570368)

    def test_schedule_rejects_permutation_extra_missing_and_bool_index(self):
        variants = [self.calls[:-1], self.calls+[self.calls[-1]]]
        swapped = copy.deepcopy(self.calls); swapped[4], swapped[5] = swapped[5], swapped[4]; variants.append(swapped)
        boolean = copy.deepcopy(self.calls); boolean[0]['call_index'] = False; variants.append(boolean)
        for value in variants:
            with self.assertRaises(BrowserGateRejected): validate_schedule(value)

    def test_phases_seed_only_first_R_each_dilation(self):
        model = PhaseCarry()
        for d in (1,3,5):
            c = next(c for c in self.calls if c['dilation'] == d)
            model.observe(c, phases(64*d))
        carry = model.transition()
        clone = PhaseCarry(carry)
        for c in [c for c in self.calls if c['stage'] == 'timing']:
            clone.observe(c, phases(64*c['dilation']))
        self.assertEqual(clone.records, carry)

    def test_phase_reset_and_drift_are_terminal_rejections(self):
        timing = self.calls[330]
        with self.assertRaisesRegex(BrowserGateRejected, 'initial_seed'): PhaseCarry().observe(timing, phases())
        model = PhaseCarry(); model.observe(self.calls[0], phases())
        with self.assertRaisesRegex(BrowserGateRejected, 'drift'): model.observe(self.calls[1], phases(64))
        with self.assertRaises(BrowserGateRejected): model.transition()

    def test_phase_rejects_invalid_alignment_and_types(self):
        for offset in (1,4096):
            with self.assertRaises(BrowserGateRejected): validate_phases({'1': phases(offset)}, complete=False)
        bad = phases(); bad[0]['region'] = False
        with self.assertRaises(BrowserGateRejected): validate_phases({'1': bad}, complete=False)

    def test_complete_qualification_mock_has_exact_lifecycles(self):
        model = LifecycleModel(self.calls, 'qualification', 0)
        for arm in ARMS: model.event('init_arm', arm=arm)
        for call in self.calls[:330]:
            model.event('prepare', call=call)
            model.event('run')
            model.event('verify')
            model.event('delete')
        model.event('shutdown')
        self.assertEqual(model.index, 330)
        self.assertEqual(model.state, 'closed')
        self.assertEqual(sum(e['command'] == 'run' for e in model.events), 330)

    def test_timing_calibration_interleaves_serial_initialization(self):
        model = LifecycleModel(self.calls, 'timing', 1)
        for arm in ARMS:
            model.event('init_arm', arm=arm); model.event('calibrate', arm=arm)
        for call in model.calls:
            model.event('prepare', call=call); model.event('run', elapsed_ms=300.0 if call['retained'] else None)
            model.event('verify'); model.event('delete')
        model.event('shutdown')
        self.assertEqual(len(model.calibrated), 5)

    def test_qualification_cannot_hide_calibration(self):
        model = LifecycleModel(self.calls, 'qualification', 0)
        model.event('init_arm', arm='R')
        with self.assertRaises(BrowserGateRejected): model.event('calibrate', arm='R')

    def test_uninitialized_next_arm_and_run_twice_rejected(self):
        model = LifecycleModel(self.calls, 'timing', 1)
        model.event('init_arm', arm='R')
        with self.assertRaisesRegex(BrowserGateRejected, 'calibrate_before'): model.event('init_arm', arm='A')
        model = LifecycleModel(self.calls, 'qualification', 0)
        for arm in ARMS: model.event('init_arm', arm=arm)
        model.event('prepare', call=self.calls[0]); model.event('run')
        with self.assertRaisesRegex(BrowserGateRejected, 'run_once'): model.event('run')

    def test_unknown_outcome_retains_attempt_and_prevents_replay(self):
        model = LifecycleModel(self.calls, 'qualification', 0)
        for arm in ARMS: model.event('init_arm', arm=arm)
        model.event('prepare', call=self.calls[0])
        with self.assertRaisesRegex(BrowserGateRejected, 'unknown_outcome'): model.event('run', acknowledged=False)
        self.assertFalse(model.events[-1]['acknowledged'])
        self.assertTrue(model.events[-1]['attempted'])
        with self.assertRaisesRegex(BrowserGateRejected, 'terminal_no_retry'): model.event('run')


class StreamingAndSource(unittest.TestCase):
    def setUp(self):
        self.controller = (ROOT/'browser/host/controller.js').read_text()
        self.worker = (ROOT/'browser/host/worker.js').read_text()

    def test_three_fixed_large_buffers_and_no_GC_dependent_tensor_copy(self):
        self.assertEqual(self.controller.count('reference = new Uint32Array(WORDS)'), 1)
        self.assertEqual(self.controller.count('readonlyScratch = new Uint8Array(READONLY_BYTES)'), 1)
        self.assertEqual(self.controller.count('packedReference = new Uint8Array(PACKED_BYTES)'), 1)
        for forbidden in ('new Uint32Array(out)', 'new Uint8Array(view)', 'HEAPU8.slice(', 'fixtureSHA256', 'outputSHA256', 'global.gc', 'Float32Array'):
            self.assertNotIn(forbidden, self.controller+self.worker)
        self.assertEqual(5*134217728+31522816+35675216+721408, configuration()['large_application_buffer_bytes'])

    def test_untouched_readonly_snapshot_cannot_be_reused_for_output_hash(self):
        model = BufferLiveness(); model.acquire('R', 1, 1); model.snapshot()
        with self.assertRaises(BrowserGateRejected): model.start_output_hash()
        model.release(); model.run()
        with self.assertRaises(BrowserGateRejected): model.acquire('R', 1, 1)
        model.acknowledge(); model.acquire('R', 2, 2); model.compare_readonly(); model.start_output_hash()
        with self.assertRaises(BrowserGateRejected): model.release()
        model.finish_hash(); model.release()

    def test_incomplete_handshake_rejected(self):
        with self.assertRaises(BrowserGateRejected): BufferLiveness().acquire('R', 3, 4)
        self.assertIn('Atomics.compareExchange(e.control, 0, QUIESCENT, INSPECTING)', self.controller)
        self.assertIn('Atomics.compareExchange(control, 0, QUIESCENT, MUTATING)', self.worker)

    def test_exact_primary_clock_and_single_run_in_that_function(self):
        body = re.search(r'function timedRun\(\) \{(.*?)\n\}', self.worker, re.S).group(1)
        self.assertEqual(body.strip(), 'const start = performance.now();\n  const native_result = mod._screen_run();\n  const end = performance.now();\n  return {native_result, elapsed_ms: end - start};')
        self.assertNotIn('canaries', body)

    def test_special_complement_poison_and_uint32_comparison(self):
        self.assertIn('out[i] = ~reference[i]', self.controller)
        self.assertIn('if (out[i] !== reference[i])', self.controller)
        self.assertIn("if (c.fixture_id === 0) for", self.controller)
        for word in (0,0x80000000,0x7fc12345,0x7fa00001,0xffffffff): self.assertNotEqual(word, (~word)&0xffffffff)

    def test_snapshot_liveness_order(self):
        body = self.controller.split('async function verify()')[1].split('function publicReceipt')[0]
        self.assertLess(body.index("'readonly_bytes'"), body.index('readonlyScratch.set(packed, 0)'))
        self.assertLess(body.index('compareBytes(packedReference'), body.index('readonlyScratch.set(packed, 0)'))
        self.assertLess(body.index('if (out[i] !== reference[i])'), body.index('readonlyScratch.set(new Uint8Array'))
        self.assertIn('await digestView(', body)

    def test_public_projection_has_no_raw_regions_or_meta(self):
        body = self.controller.split('function publicReceipt(record)')[1].split('async function handle')[0]
        self.assertNotIn('prepared', body)
        self.assertNotIn('ptr', body)
        self.assertNotIn('shared_heap', body)
        self.assertIn('duration_ms', body)

    def test_javascript_source_interlocks_before_module_import(self):
        for source in (self.controller, self.worker):
            self.assertNotIn('const SOURCE_ONLY', source)
            self.assertIn("self.location.hostname !== '127.0.0.1'", source)
            self.assertIn('self.crossOriginIsolated !== true', source)
            handler = source.split('async function handle(data) {',1)[1]
            self.assertTrue(handler.lstrip().startswith('sourceAdmission();'))
        self.assertEqual(self.worker.count('importScripts('), 1)

    def test_alias_probe_uses_same_offset_and_all_heap_leases(self):
        body = self.controller.split('async function proveDistinctBacking(')[1].split('function byteView')[0]
        self.assertIn('for (const [a, e] of entries)', body)
        self.assertIn('new Uint32Array(e.heap, ptr, 1)', body)
        self.assertIn('finally { Atomics.store(own, 0, original); }', body)
        # Tiny Python mock demonstrates why object wrapper identity is not enough.
        memory = [123,456]; a = memory; b = memory
        prior = a[1]; original = b[1]; b[1] = (~original)&0xffffffff
        self.assertNotEqual(a[1],prior)
        b[1] = original
        self.assertEqual(a[1],prior)

    def test_known_run_precedes_heap_check_and_safe_diagnostics(self):
        assign = self.worker.index('completedRun = {completed: true')
        heapread = self.worker.index('completedRun.heap_after_bytes = mod.HEAPU8.byteLength')
        self.assertLess(assign, heapread)
        self.assertIn('try { if (mod) boundedDiagnostics = diagnostics(); } catch', self.worker)
        self.assertIn('if (run && !run.acknowledged)',self.controller)

    def test_frozen_witnesses_and_nonpadding_sentinel(self):
        self.assertIn("gate(ptr !== rs[6].ptr, 'nonpadding_not_zero_sentinel'", self.controller)
        self.assertIn('const offsets = {1:[8,21,34,47],3:[48,17,22,15],5:[28,29,30,31]}', self.controller)
        self.assertIn('channel % 2 === 0 ? 0x80000000 : 0', self.controller)
        self.assertIn("'fixture_full_finite'", self.controller)
        self.assertIn("'fixture_full_zero'", self.controller)
        self.assertIn('descriptor: m.slice(0, 13)', self.controller)
        public = self.controller.split('function publicReceipt(record)')[1].split('async function handle')[0]
        self.assertNotIn('participant_ids',public)

    def test_index_only_loads_controller(self):
        html = (ROOT/'browser/host/index.html').read_text()
        self.assertEqual(re.findall(r'<script src="([^"]+)"', html), ['/controller.js'])

    def test_worker_urls_match_served_assets_root(self):
        self.assertIn("const prefix = '/' + arm + '/';", self.worker)
        self.assertNotIn("const prefix = '/assets/'", self.worker)
        for arm in ARMS:
            served_root = Path('/compile/assets')
            self.assertEqual(served_root/(("/"+arm+"/screen.js").lstrip('/')), Path('/compile/assets')/arm/'screen.js')

    def test_host_manifest_is_separate_and_complete(self):
        manifest = json.loads((ROOT/'browser/host-manifest.json').read_text())
        self.assertEqual(manifest['schema'], 'panel-activation-host-files-v1')
        self.assertEqual([row['name'] for row in manifest['files']], ['controller.js','index.html','worker.js'])
        for row in manifest['files']:
            raw = (ROOT/'browser/host'/row['name']).read_bytes()
            self.assertEqual(row, dict(name=row['name'], bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        self.assertNotIn('screen.worker.js', self.worker)
        self.assertIn("mainScriptUrlOrBlob: new URL(prefix + 'screen.js'", self.worker)
        self.assertIn("gate(name === 'screen.wasm', 'asset_name')", self.worker)


class ClockAndResourceModels(unittest.TestCase):
    def test_same_job_installation_archive_support_and_receipt_are_all_bound(self):
        installed={'chrome_support_tree_sha256':'1'*64,'chrome_executable_sha256':'2'*64}
        expected={**installed,'chrome_distribution_sha256':'3'*64,'browser_installation_receipt_sha256':'4'*64}
        self.assertEqual(validate_browser_installation_binding(installed,'3'*64,'4'*64,expected),expected)
        for key in expected:
            changed=copy.deepcopy(expected);changed[key]='f'*64
            with self.assertRaises(BrowserGateRejected): validate_browser_installation_binding(installed,'3'*64,'4'*64,changed)
        with self.assertRaises(BrowserGateRejected): validate_browser_installation_binding({'chrome_executable_sha256':'2'*64},'3'*64,'4'*64,expected)
        with self.assertRaises(BrowserGateRejected): validate_browser_installation_binding(installed,'3'*64,None,expected)

    def test_probe_cleanup_clock_excludes_launch_but_keeps_both_caps(self):
        self.assertEqual(validate_probe_timing(45.0,2.0),2.0)
        self.assertEqual(validate_probe_timing(60.0,30.0),30.0)
        for probe,cleanup in ((60.001,2),(45,30.001),(10,11),(45,None),(45,False)):
            with self.assertRaises(BrowserGateRejected): validate_probe_timing(probe,cleanup)
        source=(ROOT/'browser/vendor/operator_browser.py').read_text()
        self.assertIn("result['cleanup_wall_seconds'] = cleanup_completed_monotonic-cleanup_started_monotonic",source)
        self.assertIn("cleanup_started = time.monotonic()\n                    active.close()",source)

    def test_nearest_rank_p99_and_censoring(self):
        value = dict(quantum_ms=.005, positive_samples=1000, reads=5000,
            empty_export_samples=1000, empty_export_p99_ms=0,
            p99_resolution_censored=True, minimum_primary_ms=1, subtracted=False)
        self.assertEqual(validate_calibration(value), 1)
        value['minimum_primary_ms'] = .9
        with self.assertRaises(BrowserGateRejected): validate_calibration(value)

    def sample(self):
        return dict(generations={'owner':'generation-1','chrome':'generation-2'},
            processes={k:dict(cpu_seconds=1,rss_bytes=1000,major_faults=0,swap_bytes=1024) for k in ('owner','chrome')},
            host=dict(available_bytes=2*1024**3,swap_in_bytes=0,swap_out_bytes=0),
            runtime_artifact_bytes=1024,public_receipt_bytes=1024)

    def test_primary_resource_gate_and_untimed_fault_disclosure(self):
        a = self.sample(); b = copy.deepcopy(a); b['processes']['chrome']['major_faults'] = 1
        with self.assertRaises(BrowserGateRejected): validate_resource_window(a,b,retained=True,stage='qualification')
        value = validate_resource_window(a,b,retained=False,stage='qualification')
        self.assertTrue(value['sampling_not_aggregate_quota'])

    def test_rss_swap_generation_missing_fail(self):
        for mutate in (
            lambda x: x['processes']['chrome'].update(rss_bytes=4*1024**3),
            lambda x: x['processes']['chrome'].update(swap_bytes=2048),
            lambda x: x['generations'].update(chrome='new-generation'),
            lambda x: x['host'].update(available_bytes=1),
            lambda x: x.update(runtime_artifact_bytes=257*1024**2)):
            a = self.sample(); b = copy.deepcopy(a); mutate(b)
            with self.assertRaises(BrowserGateRejected): validate_resource_window(a,b,retained=True,stage='timing')

    def test_unknown_cache_metadata_adds_no_gate(self):
        self.assertEqual(optional_cache_metadata(None), {'status':'unknown','caches':[]})
        self.assertFalse(optional_cache_metadata([{'level':1,'type':'Data','line_bytes':64,'size_bytes':49152}])['performance_gate'])

    def test_profile_and_temp_scope_is_owned_and_flags_are_exact(self):
        actual = ['/pinned/chrome','--js-flags=--no-wasm-revectorize','--user-data-dir=/tmp/test-owned/tmp/playwright_chromiumdev_profile-ABC123']
        policy = ['/pinned/chrome','--js-flags=--no-wasm-revectorize','--user-data-dir=<OWNED_PROFILE_DIR>']
        self.assertEqual(len(validate_launch_argv(actual,policy,'/tmp/test-owned/tmp')),64)
        with self.assertRaises(BrowserGateRejected): validate_launch_argv(actual,policy,'/tmp/unowned')
        actual[1] += ' --liftoff-only'
        with self.assertRaises(BrowserGateRejected): validate_launch_argv(actual,policy,'/tmp/test-owned/tmp')

    def test_identical_missing_or_boolean_host_observations_rejected(self):
        for invalid in (None,False,float('nan'),-1):
            a = self.sample(); a['host']['swap_in_bytes'] = invalid
            with self.assertRaises(BrowserGateRejected): validate_resource_window(a,a,retained=False,stage='qualification')


if __name__ == '__main__':
    unittest.main()
