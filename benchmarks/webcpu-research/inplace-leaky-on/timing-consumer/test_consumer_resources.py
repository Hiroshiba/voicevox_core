"""Synthetic process filesystem and report attacks; not historical-run evidence."""
import copy, io, re, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import consumer_resources as resources
import consumer_gate as gate
import quality_gate
import timing_validate as timing
from test_consumer import synthetic_transfer_timing
from source_resources import resource_observations, idle_cpu

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.births = {10: 10.5, 20: 20.5, 21: 21.5, 30: 30.5}
        for pid, parent, children in [(10, 1, '20'), (20, 10, '30'), (21, 10, ''), (30, 20, '')]:
            folder = self.root / str(pid); (folder / 'task' / str(pid)).mkdir(parents=True)
            fields = ['S', str(parent)] + ['0'] * 20
            (folder / 'stat').write_text(f'{pid} (synthetic chrome) ' + ' '.join(fields))
            (folder / 'status').write_text('VmSwap:\t0 kB\n')
            (folder / 'task' / str(pid) / 'children').write_text(children)
        (self.root / '10/task/11').mkdir()
        (self.root / '10/task/11/children').write_text('21')
    def tearDown(self):
        self.temporary.cleanup()
    def factory(self, pid):
        return SimpleNamespace(create_time=lambda: self.births[pid],
                               cpu_times=lambda: SimpleNamespace(_asdict=lambda: {k: 0 for k in resources.CPU_FIELDS}),
                               memory_info=lambda: SimpleNamespace(rss=4096))
    def collect(self, factory=None):
        return resources.collect(10, 10.5, process_factory=factory or self.factory, proc_root=self.root)
    def test_multiple_threads_children_and_grandchildren(self):
        rows = self.collect()
        self.assertEqual([x['pid'] for x in rows], [10, 20, 21, 30])
        self.assertEqual(len([x for x in rows if (x['pid'], x['created']) == (10, 10.5)]), 1)
        self.assertTrue(all(set(x['cpu']) == resources.CPU_FIELDS for x in rows))
    def test_stable_child_read_failure_is_not_omitted(self):
        def factory(pid):
            process = self.factory(pid)
            if pid == 20:
                def denied(): raise PermissionError('synthetic denied child')
                process.cpu_times = denied
            return process
        with self.assertRaises(PermissionError): self.collect(factory)
        (self.root / '20/status').unlink()
        with self.assertRaises(FileNotFoundError): self.collect()
    def test_psutil_access_denied_and_no_such_process_never_return_root_only(self):
        import psutil
        for exception in [psutil.AccessDenied(20), psutil.NoSuchProcess(20)]:
            def factory(pid):
                process = self.factory(pid)
                if pid == 20:
                    def interrupted(): raise exception
                    process.cpu_times = interrupted
                return process
            with self.assertRaises(type(exception)): self.collect(factory)
    def test_incomplete_enumeration_fails_closed(self):
        (self.root / '10/task/11/children').unlink()
        with self.assertRaises(FileNotFoundError): self.collect()
    def test_tree_mutation_during_collection_rejected(self):
        def factory(pid):
            process = self.factory(pid)
            if pid == 30:
                def memory():
                    (self.root / '10/task/11/children').write_text('')
                    return SimpleNamespace(rss=4096)
                process.memory_info = memory
            return process
        with self.assertRaisesRegex(ValueError, 'tree changed'): self.collect(factory)
    def test_pid_reuse_and_wrong_root_generation_rejected(self):
        self.births[10] = 11
        with self.assertRaisesRegex(ValueError, 'Browser process generation'): self.collect()
        self.births[10] = 10.5
        def factory(pid):
            process = self.factory(pid)
            if pid == 20:
                def memory():
                    self.births[20] += 1
                    return SimpleNamespace(rss=4096)
                process.memory_info = memory
            return process
        with self.assertRaisesRegex(ValueError, 'generation changed'): self.collect(factory)
    def test_missing_swap_or_cpu_counter_rejected(self):
        (self.root / '20/status').write_text('Name: child\n')
        with self.assertRaisesRegex(ValueError, 'swap coverage'): self.collect()
        (self.root / '20/status').write_text('VmSwap: 0 kB\n')
        def factory(pid):
            process = self.factory(pid)
            process.cpu_times = lambda: SimpleNamespace(_asdict=lambda: {'user': 0, 'system': 0})
            return process
        with self.assertRaisesRegex(ValueError, 'CPU counter coverage'): self.collect(factory)
    def test_self_test_child_readiness_is_bounded_and_cleaned_up(self):
        child = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO('ready\n'), wait=Mock(), kill=Mock(), pid=20)
        with patch('subprocess.Popen', return_value=child), patch('select.select', return_value=([], [], [])) as wait:
            with self.assertRaisesRegex(ValueError, 'readiness timed out'): resources.self_test()
        self.assertEqual(wait.call_args.args[-1], 10)
        child.wait.assert_called_once_with(timeout=10)
        self.assertTrue(child.stdin.closed and child.stdout.closed)

class CoverageTests(unittest.TestCase):
    def rows_for(self, report, group_id, mode):
        group = report['process_sets'][group_id - 1]
        idle = group['idle']['modes'][mode]
        rows = [idle['before'], idle['after']]
        for trial in report['trials']:
            if (trial['process_set'], trial['mode']) == (group_id, mode):
                rows += [trial['before']['processes'], trial['after']['processes']]
        rows += [group['coverage_final'][mode]['processes']]
        return rows
    def derive(self, report):
        for group in report['process_sets']:
            for idle in group['idle']['modes'].values():
                idle.update(idle_cpu(idle['before'], idle['after'], group['idle']['interval_s']))
        for row in report['trials']:
            row['resource_observations'] = resource_observations(row['before'], row['after'])
    def rejected(self, report, contract):
        self.derive(report)
        with patch.object(gate, 'load_contract', return_value=contract):
            summary = timing.summarize(report)
            self.assertEqual(summary['status'], 'inconclusive_invalid_quality')
            self.assertFalse(summary['performance_claim_allowed'])
            self.assertEqual(len(report['trials']), 27)
            with self.assertRaises(ValueError): quality_gate.verify(report, summary)
    def test_actual_root_omitted_from_all_54_trial_snapshots(self):
        report, contract = synthetic_transfer_timing()
        for row in report['trials']:
            for side in ['before', 'after']:
                row[side]['processes'][0].update(pid=99999, created=1234)
        self.rejected(report, contract)
    def test_stable_wrong_root_pid_or_birthtime_everywhere_rejected(self):
        for field, value in [('pid', 99999), ('created', 99999)]:
            report, contract = synthetic_transfer_timing()
            for rows in self.rows_for(report, 1, 'original'):
                rows[0][field] = value
            self.rejected(report, contract)
    def test_missing_root_but_stable_descendant_is_rejected(self):
        report, contract = synthetic_transfer_timing()
        for rows in self.rows_for(report, 1, 'original'):
            rows[0].update(pid=99999, created=99999)
        self.rejected(report, contract)
    def test_duplicate_process_identity_rejected(self):
        report, contract = synthetic_transfer_timing()
        for rows in self.rows_for(report, 1, 'original'):
            rows.append(copy.deepcopy(rows[0]))
        self.rejected(report, contract)
    def test_idle_to_first_call_and_between_calls_changed_generation(self):
        for index in [2, 4]:
            report, contract = synthetic_transfer_timing()
            rows = self.rows_for(report, 1, 'original')
            # Keep the browser root; a child appears only after the indicated boundary.
            for rowset in rows[index:]:
                child = copy.deepcopy(rowset[0]); child.update(pid=99999, created=99999); rowset.append(child)
            self.rejected(report, contract)
    def test_end_sentinel_continuity_and_swap_coverage(self):
        report, contract = synthetic_transfer_timing()
        final = report['process_sets'][0]['coverage_final']['original']
        child = copy.deepcopy(final['processes'][0]); child.update(pid=99999, created=99999); final['processes'].append(child)
        self.rejected(report, contract)
        report, contract = synthetic_transfer_timing()
        report['process_sets'][0]['coverage_final']['original']['host']['swap_in'] = 1
        self.rejected(report, contract)
    def test_same_descendant_cannot_belong_to_two_browser_generations(self):
        report, contract = synthetic_transfer_timing()
        for group_id, mode in [(1, 'original'), (2, 'rebuilt')]:
            for rows in self.rows_for(report, group_id, mode):
                child = copy.deepcopy(rows[0]); child.update(pid=99999, created=99999); rows.append(child)
        self.rejected(report, contract)
    def test_multiple_descendants_and_nine_distinct_browser_generations_pass(self):
        report, contract = synthetic_transfer_timing()
        for group in report['process_sets']:
            for mode, runtime in group['runtime'].items():
                for rows in self.rows_for(report, group['id'], mode):
                    for offset in [10000, 20000]:
                        child = copy.deepcopy(rows[0]); child.update(pid=runtime['browser_pid'] + offset, created=runtime['browser_created'] + offset)
                        rows.append(child)
        self.derive(report)
        with patch.object(gate, 'load_contract', return_value=contract):
            summary = timing.summarize(report)
            self.assertTrue(summary['quality']['consumer_process_coverage']['passed'])
            self.assertTrue(quality_gate.verify(report, summary))
    def test_runner_uses_strict_collector_and_final_boundary(self):
        source = (Path(__file__).parent.parent / 'timing-release/timing_confirmation.py').read_text()
        self.assertIn('collect_usage = consumer_resources.collect', source)
        self.assertFalse(re.search(r'(?<![A-Za-z_])usage\(pids\[', source))
        self.assertGreater(source.index("group['coverage_final'] ="), source.index("'position': 'after'"))
        workflow = (Path(__file__).resolve().parents[4] / '.github/workflows/benchmark-webcpu-inplace-leaky-on-consumer.yml').read_text()
        self.assertLess(workflow.index('consumer_resources.py --self-test'), workflow.index('timing_confirmation.py'))
        self.assertLess(workflow.index('consumer_resources.py --self-test'), workflow.index('consumer_artifact.py'))
        self.assertLess(workflow.index('consumer_resources.py --self-test'), workflow.index('prepare_private_model.py'))

if __name__ == '__main__': unittest.main()
