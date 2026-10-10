"""Separate TEST-ONLY transfer fixtures. No production pins or measurement evidence."""
import copy, fnmatch, json, re, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'timing-release'))
sys.path.insert(0, str(ROOT.parent / 'code-transfer'))
import consumer_gate as gate
import consumer_resources
import quality_gate
import timing_validate as timing
import test_code_transfer as transfer_tests
from test_timing_release import timing_fixture
from source_resources import resource_observations

H = 'a' * 64

def synthetic_contract(report):
    """Only called by tests; never read by a production entry point."""
    c = json.loads((ROOT / 'consumer_contract.json').read_bytes())
    c['status'] = 'reviewed'
    c['code_transfer'] = {'producer': {'repository': gate.REPOSITORY, 'run_id': 123, 'commit': 'a' * 40},
                          'artifact_id': 456, 'artifact_zip_sha256': H, 'transfer_sha256': H}
    c['capture'] = {'producer': c['code_transfer']['producer'], 'artifact_id': 789, 'artifact_zip_sha256': H, 'report_sha256': H, 'summary_sha256': H,
                    'evidence_hashes': {m: H for m in gate.KEYS}, 'profiled_native_capture': True, 'uninstrumented_code_identity_claimed': False}
    c['reviews'] = [{'file': name, 'sha256': char * 64, 'passed': True, 'capture_report_sha256': H, 'transfer_sha256': H}
                    for name, char in zip(gate.REVIEW_FILES, ['a', 'b'])]
    c['engine'] = {k: report['activation']['original']['on'][k] for k in ['product', 'js_version']}
    c['reference'] = copy.deepcopy(report['reference'])
    c['identities'] = {m: {k: report['provenance'][m][k] for k in gate.IDENTITY_FIELDS} for m in gate.KEYS}
    c['provenance_sha256'] = gate.sha(gate.canonical(report['provenance']))
    c['source_proof_sha256'] = gate.sha(gate.canonical(report['source_proof']))
    return c

def synthetic_transfer_timing():
    report = timing_fixture()
    report['schema'] = 'inplace-leaky-on-exact-code-timing-v1'
    report['consumer_process_coverage_policy'] = consumer_resources.POLICY
    for group in report['process_sets']:
        group['coverage_final'] = {}
        for mode, runtime in group['runtime'].items():
            def bind(rows):
                for row in rows:
                    row.update(pid=runtime['browser_pid'], created=runtime['browser_created'])
                    row['cpu'].update(children_user=0, children_system=0, iowait=0)
            idle = group['idle']['modes'][mode]
            bind(idle['before']); bind(idle['after'])
            for row in report['trials']:
                if (row['process_set'], row['mode']) == (group['id'], mode):
                    bind(row['before']['processes']); bind(row['after']['processes'])
                    row['resource_observations'] = resource_observations(row['before'], row['after'])
                    group['coverage_final'][mode] = copy.deepcopy(row['after'])
    c = synthetic_contract(report)
    with patch.object(gate, 'load_contract', return_value=c):
        report['code_transfer'] = gate.expected_receipt(c)
        report['release_gate'] = gate.release_gate(report['provenance'], c['engine'], c['reference'])
    return report, c

class ConsumerContracts(unittest.TestCase):
    def test_production_requires_real_review_or_remains_closed(self):
        c = json.loads((ROOT / 'consumer_contract.json').read_bytes())
        if c['status'] != 'reviewed':
            with self.assertRaisesRegex(ValueError, 'Fresh capture'):
                gate.load_contract()
            self.assertIsNone(c['code_transfer']['artifact_id'])
            self.assertIsNone(c['identities'])
            self.assertTrue(all(x['passed'] is False for x in c['reviews']))
        else:
            # A future real release must validate the separately hashed review files.
            self.assertEqual(gate.load_contract(), c)
            self.assertTrue(all(x['passed'] is True for x in c['reviews']))

    def test_historical_contract_unchanged(self):
        self.assertEqual(gate.sha((ROOT.parent / 'timing-release/release_contract.json').read_bytes()), gate.HISTORICAL_CONTRACT_SHA)
        historical = timing_fixture()
        self.assertNotIn('code_transfer', historical)
        with patch.object(timing, 'release_gate', return_value={'synthetic': True}):
            self.assertTrue(timing.validate(historical))

    def test_separate_synthetic_transfer_fixture(self):
        report, c = synthetic_transfer_timing()
        gate.validate_contract(c)
        with patch.object(gate, 'load_contract', return_value=c):
            self.assertTrue(timing.validate(report))
            self.assertTrue(quality_gate.verify(report, timing.summarize(report)))
        self.assertFalse(report['code_transfer']['downloaded_zip_digest_hard_verified'])
        self.assertFalse(report['code_transfer']['source_and_archive_proofs_recomputed_here'])

    def test_unreviewed_or_changed_contract_rejected(self):
        _, base = synthetic_transfer_timing()
        changes = [lambda c: c.update(status='pending'), lambda c: c.update(primary_calls=26),
                   lambda c: c.update(primary_flags=['--js-flags=--wasm-revectorize --no-liftoff']),
                   lambda c: c['code_transfer'].update(artifact_id=None), lambda c: c['code_transfer'].update(artifact_zip_sha256=None),
                   lambda c: c['code_transfer'].update(transfer_sha256=None), lambda c: c['code_transfer']['producer'].update(repository='VOICEVOX/voicevox_core'),
                   lambda c: c['capture'].update(producer={}), lambda c: c['capture'].update(report_sha256=None),
                   lambda c: c['capture'].update(uninstrumented_code_identity_claimed=True),
                   lambda c: c['reviews'][0].update(passed=False), lambda c: c['reviews'][1].update(passed=False),
                   lambda c: c['reviews'][1].update(sha256=c['reviews'][0]['sha256']),
                   lambda c: c['reviews'][0].update(transfer_sha256='b' * 64), lambda c: c['reviews'][1].update(capture_report_sha256='b' * 64),
                   lambda c: c['reviews'][0].update(file='../MANUAL_REVIEW.md'), lambda c: c.update(model_sha256='b' * 64),
                   lambda c: c.update(quality_policy={}), lambda c: c['identities']['candidate'].update(callback_body_sha256=gate.ORIGINAL_BODY),
                   lambda c: c.update(secret='unapproved')]
        for change in changes:
            c = copy.deepcopy(base); change(c)
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError, KeyError)):
                gate.validate_contract(c)

    def test_bound_receipt_and_producer_proofs_rejected_on_change(self):
        base, c = synthetic_transfer_timing()
        changes = [lambda r: r['code_transfer'].update(artifact_id=789), lambda r: r['code_transfer'].update(artifact_api_digest='sha256:' + 'b' * 64),
                   lambda r: r['code_transfer'].update(transfer_sha256='b' * 64), lambda r: r['code_transfer'].update(downloaded_zip_digest_hard_verified=True),
                   lambda r: r['code_transfer'].update(source_and_archive_proofs_recomputed_here=True), lambda r: r['code_transfer']['producer'].update(run_id=999),
                   lambda r: r['code_transfer']['producer'].update(commit='b' * 40), lambda r: r['provenance']['original'].update(receipt_sha256='b' * 64),
                   lambda r: r['source_proof'].update(manifest_sha256='b' * 64), lambda r: r['release_gate'].update(manual_review_pass=False)]
        for change in changes:
            report = copy.deepcopy(base); change(report)
            with self.subTest(change=change), patch.object(gate, 'load_contract', return_value=c), self.assertRaises(ValueError):
                timing.validate(report)

    def test_stripped_receipt_cannot_fall_back_to_historical_mode(self):
        report, c = synthetic_transfer_timing()
        del report['code_transfer']
        with patch.object(gate, 'load_contract', return_value=c), self.assertRaises(ValueError):
            timing.validate(report)
        report['schema'] = 'inplace-leaky-on-reviewed-timing-v1'
        with self.assertRaises(ValueError): timing.validate(report)
        historical = timing_fixture(); historical['code_transfer'] = {}
        with self.assertRaises(ValueError): timing.validate(historical)

    def test_mutated_source_after_preflight_never_reaches_activation_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'runtime'; destination = root / 'served'
            source.write_bytes(b'reviewed runtime'); pin = gate.sha(source.read_bytes())
            source.write_bytes(b'mutated runtime')
            with self.assertRaisesRegex(ValueError, 'changed after transfer preflight'):
                gate.copy_verified_file(source, destination, pin, 1024)
            self.assertFalse(destination.exists())
            source.write_bytes(b'reviewed runtime')
            gate.copy_verified_file(source, destination, pin, 1024)
            source.write_bytes(b'later source mutation')
            self.assertEqual(destination.read_bytes(), b'reviewed runtime')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o444)

    def test_expired_or_wrong_artifact_origin_rejected(self):
        _, c = synthetic_transfer_timing()
        base = {'id': 456, 'name': 'webcpu-pristine-on-immutable-code', 'expired': False, 'digest': 'sha256:' + H,
                'workflow_run': {'id': 123, 'head_sha': 'a' * 40, 'repository_id': 617625583, 'head_repository_id': 617625583}}
        mutations = [lambda m: m.update(expired=True), lambda m: m.update(id=457), lambda m: m.update(digest='sha256:' + 'b' * 64),
                     lambda m: m['workflow_run'].update(id=124), lambda m: m['workflow_run'].update(head_sha='b' * 40),
                     lambda m: m['workflow_run'].update(repository_id=1234), lambda m: m['workflow_run'].update(head_repository_id=1234)]
        for mutate in mutations:
            metadata = copy.deepcopy(base); mutate(metadata)
            with self.assertRaises(ValueError): gate.validate_artifact(metadata, c['code_transfer'])
        bad = copy.deepcopy(c['code_transfer']); bad['producer']['repository'] = 'private-fork/voicevox_core'
        with self.assertRaises(ValueError): gate.validate_artifact(base, bad)

    def test_quality_gate_rejects_forged_summary(self):
        report, c = synthetic_transfer_timing()
        with patch.object(gate, 'load_contract', return_value=c):
            summary = timing.summarize(report)
            for key, value in [('primary_calls', 26), ('performance_claim_allowed', False), ('status', 'invented')]:
                changed = copy.deepcopy(summary); changed[key] = value
                with self.assertRaises(ValueError): quality_gate.verify(report, changed)
            report['process_sets'][0]['sentinels'][1]['elapsed_s'] = 2
            with self.assertRaises(ValueError): quality_gate.verify(report, summary)
            invalid = timing.summarize(report)
            self.assertEqual(invalid['status'], 'inconclusive_invalid_quality')
            self.assertEqual(len(report['trials']), 27)
            self.assertFalse(invalid['performance_claim_allowed'])
            with self.assertRaises(ValueError): quality_gate.verify(report, invalid)

    def test_transfer_fixture_quality_cases_and_boundaries(self):
        cases = [lambda r: r['trials'][0]['after']['processes'][0].update(pid=999),
                 lambda r: r['trials'][0]['after']['processes'][0].update(created=999),
                 lambda r: r['trials'][0]['after']['processes'][0].update(rss=None),
                 lambda r: r['trials'][0]['after']['processes'][0].update(major_faults=None),
                 lambda r: r['trials'][0]['after']['processes'][0].update(minor_faults=None),
                 lambda r: r['trials'][0]['after']['processes'][0].update(swap_bytes=None),
                 lambda r: r['trials'][0]['before']['processes'][0].update(major_faults=1),
                 lambda r: r['trials'][0]['before']['processes'][0].update(minor_faults=1),
                 lambda r: r['trials'][0]['after']['processes'][0]['cpu'].update(user=0),
                 lambda r: r['trials'][0]['after']['processes'][0].update(swap_bytes=1),
                 lambda r: r['trials'][0]['after']['host'].update(swap_in=1),
                 lambda r: r['trials'][0]['after']['host'].update(swap_out=1),
                 lambda r: r['trials'][0]['after']['host'].update(swap_used=1),
                 lambda r: r['trials'][0]['before']['host'].update(swap_in=1),
                 lambda r: r['trials'][0]['after']['host'].pop('total_bytes'),
                 lambda r: r['process_sets'][0]['idle']['modes']['original']['after'][0].update(rss=None),
                 lambda r: r['process_sets'][0]['sentinels'][1].update(elapsed_s=1.100001),
                 lambda r: r['process_sets'][0]['sentinels'][1].update(elapsed_s=0.899999)]
        for change in cases:
            report, c = synthetic_transfer_timing(); change(report)
            for row in report['trials']:
                row['resource_observations'] = resource_observations(row['before'], row['after'])
            with self.subTest(change=change), patch.object(gate, 'load_contract', return_value=c):
                summary = timing.summarize(report)
                self.assertFalse(summary['performance_claim_allowed'])
                self.assertEqual(len(report['trials']), 27)
                with self.assertRaises(ValueError): quality_gate.verify(report, summary)
        for ratio in [0.9, 1.1]:
            report, c = synthetic_transfer_timing()
            report['process_sets'][0]['sentinels'][1]['elapsed_s'] = ratio
            report['trials'][0]['before']['host']['available_bytes'] = 1024 ** 3
            with patch.object(gate, 'load_contract', return_value=c):
                self.assertTrue(quality_gate.verify(report, timing.summarize(report)))
        report, c = synthetic_transfer_timing()
        report['trials'][0]['before']['host']['available_bytes'] = 1024 ** 3 - 1
        with patch.object(gate, 'load_contract', return_value=c), self.assertRaises(ValueError):
            timing.summarize(report)

    def test_major_fault_mask_common_across_three_comparisons(self):
        report, c = synthetic_transfer_timing(); seen = False
        for row in report['trials']:
            if row['process_set'] == 1 and row['mode'] == 'candidate':
                row['before']['processes'][0]['major_faults'] = int(seen)
                row['after']['processes'][0]['major_faults'] = 1; seen = True
                row['resource_observations'] = resource_observations(row['before'], row['after'])
        report['process_sets'][0]['coverage_final']['candidate']['processes'][0]['major_faults'] = 1
        with patch.object(gate, 'load_contract', return_value=c):
            summary = timing.summarize(report)
            self.assertTrue(quality_gate.verify(report, summary))
        self.assertEqual(summary['primary_calls'], 27)
        self.assertEqual(summary['fault_sensitivity_remaining_rounds'], 8)
        masks = []
        for comparison in summary['paired_effects'].values():
            self.assertEqual(len(comparison['pairs']), 9)
            masks.append([(p['process_set'], p['pair']) for p in comparison['pairs'] if p['fault_exposed']])
            self.assertEqual(comparison['fault_sensitivity'][0]['observations'], 8)
        self.assertEqual(masks[0], masks[1]); self.assertEqual(masks[1], masks[2])

    def test_between_calls_and_idle_quality_cannot_be_hidden(self):
        mutations = [lambda r: r['initial_host'].update(swap_in=1),
                     lambda r: r['initial_host'].pop('total_bytes'),
                     lambda r: r['process_sets'][0]['idle']['modes']['original']['after'][0].update(pid=999),
                     lambda r: r['process_sets'][0]['idle']['modes']['original']['after'].append(copy.deepcopy(r['process_sets'][0]['idle']['modes']['original']['after'][0])),
                     lambda r: r['trials'][0]['after']['processes'][0]['cpu'].pop('children_user'),
                     lambda r: r['trials'][0]['after']['processes'].append(copy.deepcopy(r['trials'][0]['after']['processes'][0]))]
        for mutate in mutations:
            report, c = synthetic_transfer_timing(); mutate(report)
            # The caller must not be able to hide instability by retaining stale derived counters.
            from source_resources import idle_cpu
            for g in report['process_sets']:
                for row in g['idle']['modes'].values():
                    row.update(idle_cpu(row['before'], row['after'], g['idle']['interval_s']))
            for row in report['trials']:
                row['resource_observations'] = resource_observations(row['before'], row['after'])
            with patch.object(gate, 'load_contract', return_value=c):
                summary = timing.summarize(report)
                self.assertFalse(summary['performance_claim_allowed'])
                with self.assertRaises(ValueError): quality_gate.verify(report, summary)
        for counter in ['swap_in', 'swap_out', 'swap_used']:
            report, c = synthetic_transfer_timing()
            for row in report['trials'][1:]:
                for side in ['before', 'after']: row[side]['host'][counter] = 1
                row['resource_observations'] = resource_observations(row['before'], row['after'])
            with patch.object(gate, 'load_contract', return_value=c):
                summary = timing.summarize(report)
                self.assertFalse(summary['performance_claim_allowed'])
                with self.assertRaises(ValueError): quality_gate.verify(report, summary)

    def test_workflow_has_explicit_three_validation_stages(self):
        workflow = (ROOT.parents[3] / '.github/workflows/benchmark-webcpu-inplace-leaky-on-consumer.yml').read_text()
        self.assertLess(workflow.index('timing_confirmation.py'), workflow.index('timing_validate.py'))
        self.assertLess(workflow.index('timing_validate.py'), workflow.index('quality_gate.py'))
        self.assertIn('artifact-ids: ${{ steps.release.outputs.artifact_id }}', workflow)
        self.assertIn('run-id: ${{ steps.release.outputs.run_id }}', workflow)
        self.assertNotIn('ci_prepare.py', workflow)
        self.assertNotIn('run_capture_only', workflow)
        self.assertNotIn('actions/cache', workflow)
        runner = (ROOT.parent / 'timing-release/timing_confirmation.py').read_text()
        self.assertLess(runner.index('consumer_gate.preflight('), runner.index("stage('activation_gates')"))
        self.assertNotIn('--perf-prof', runner); self.assertNotIn('no-liftoff', runner)

    def test_producer_and_consumer_push_scopes_are_separate(self):
        workflow_root = ROOT.parents[3] / '.github/workflows'
        producer = (workflow_root / 'benchmark-webcpu-inplace-leaky-on-screen.yml').read_text()
        consumer = (workflow_root / 'benchmark-webcpu-inplace-leaky-on-consumer.yml').read_text()
        def paths(text):
            return re.findall(r"^      - '([^']+)'$", text.split('permissions:')[0], re.MULTILINE)
        producer_paths, consumer_paths = paths(producer), paths(consumer)
        def matches(path, patterns): return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
        prefix = 'benchmarks/webcpu-research/inplace-leaky-on/'
        publication = [prefix + 'timing-consumer/consumer_gate.py', prefix + 'timing-release/timing_confirmation.py',
                       '.github/workflows/benchmark-webcpu-inplace-leaky-on-consumer.yml',
                       '.github/workflows/benchmark-webcpu-inplace-leaky-on-screen.yml']
        self.assertFalse(any(matches(path, producer_paths) for path in publication))
        self.assertTrue(any(matches(path, consumer_paths) for path in publication))
        for folder in ['ci', 'code-transfer', 'jit-capture', 'preparation', 'browser', 'local-gates', 'diagnostics', 'extracted']:
            self.assertTrue(matches(prefix + folder + '/source.py', producer_paths))
            self.assertFalse(matches(prefix + folder + '/source.py', consumer_paths))
        self.assertTrue(matches(prefix + 'ci_prepare.py', producer_paths))
        for text in [producer, consumer]:
            self.assertIn('  workflow_dispatch:', text)
            self.assertIn("branches: ['benchmark/native-browser-research-20261008']", text)

class ExactByteTransferTests(transfer_tests.Tests):
    """Reuse isolated synthetic Wasm-parser fixture; never real evidence."""
    def test_exact_transfer_bytes_license_and_extra_directory(self):
        with self.context():
            receipt = self.export(); model = self.root / 'model'; model.write_bytes(b'synthetic-model')
            metadata = self.root / 'metadata.json'
            report = timing_fixture(); report['provenance'] = self.prov; report['source_proof'] = self.proof
            c = synthetic_contract(report); c['code_transfer']['producer'] = self.prod
            c['code_transfer']['transfer_sha256'] = receipt['transfer_sha256']
            metadata.write_text(json.dumps({'id': 456, 'name': 'webcpu-pristine-on-immutable-code', 'expired': False, 'digest': 'sha256:' + H,
                'workflow_run': {'id': 123, 'head_sha': 'a' * 40, 'repository_id': 617625583, 'head_repository_id': 617625583}}))
            with patch.object(gate, 'load_contract', return_value=c), patch.object(transfer_tests.m, 'MODEL_SHA', transfer_tests.m.sha(model.read_bytes())):
                entries, _, _, consumer = gate.preflight(self.out, model, metadata)
                self.assertEqual(set(entries), set(gate.KEYS)); self.assertEqual(consumer['artifact_id'], 456)
                transfer = self.out / 'transfer.json'; raw = transfer.read_bytes(); transfer.write_bytes(raw + b' ')
                with self.assertRaises(ValueError): gate.preflight(self.out, model, metadata)
                transfer.write_bytes(raw)
                notice = self.out / 'LICENSES.txt'; raw = notice.read_bytes(); notice.write_bytes(raw + b'changed')
                with self.assertRaises(ValueError): gate.preflight(self.out, model, metadata)
                notice.write_bytes(raw)
                (self.out / 'empty-extra').mkdir()
                with self.assertRaises(ValueError): gate.preflight(self.out, model, metadata)

if __name__ == '__main__':
    unittest.main()
