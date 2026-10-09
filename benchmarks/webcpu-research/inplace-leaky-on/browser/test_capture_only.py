"""Capture transport/schema fixtures; no browser, build or inference execution."""
import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import source_capture as capture
import source_native
import source_validate
from source_manifest import KEYS
from test_browser import fixture

source_native.proof_module()  # Adds the immutable shared parser fixture directory.
import test_source_native_proof as native_fixture


def capture_fixture():
    report = fixture()
    report.update(schema='inplace-leaky-on-capture-v1', capture_only=True, status='awaiting_manual_review',
                  stage='awaiting_manual_review', gates_complete_before_timing=False,
                  private_temporary_directory_deleted=False, private_native_dump_retained=True,
                  trials=[], process_sets=[])
    for mode, row in report['native_gate'].items():
        row.update(schema='inplace-leaky-browser-native-capture-v1', verified=False,
                   capture_verified=True, semantic_verified=False,
                   native_extraction_helper_sha256=capture.EXTRACTION_SHA256)
        row.pop('proof')
        row.pop('native_proof_helper_sha256')
        dump = native_fixture.dump(native_fixture.fixture(mode), index=row['callback_function_index'])
        row['code_evidence'] = capture.capture_target_code(dump, row['callback_function_index'], mode)
    return report


class CaptureOnlyContracts(unittest.TestCase):
    def test_capture_has_no_semantic_analysis_or_primary_calls(self):
        extractor = capture.extraction_module()
        with patch.object(extractor, 'analyze_instructions', side_effect=AssertionError('Semantic analyzer must not run')):
            report = capture_fixture()
            self.assertTrue(source_validate.validate(report, capture_only=True))
        self.assertEqual(report['trials'], [])
        self.assertEqual(report['process_sets'], [])
        with self.assertRaises(ValueError):
            source_validate.validate(report)

    def test_only_isolated_lexer_encoding_projection_changed(self):
        shared = ast.parse(Path(source_native.proof_module().__file__).read_text())
        isolated = ast.parse(Path(capture.extraction_module().__file__).read_text())
        select = lambda tree: next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'analyze_instructions')
        self.assertEqual(ast.dump(select(shared)), ast.dump(select(isolated)))
        text = native_fixture.dump(native_fixture.fixture())
        parser = capture.extraction_module()
        default = parser.parse_blocks(text, 8227)
        encoded = parser.parse_blocks(text, 8227, include_encoding=True)
        for block in encoded:
            for row in block['instructions']:
                row.pop('encoding_hex')
        self.assertEqual(default, encoded)

    def test_rex_annotation_requires_corresponding_encoding(self):
        text = ('--- WebAssembly code ---\nname: wasm-function[7]\nindex: 7\nkind: wasm function\ncompiler: TurboFan\n'
                'Instructions (size = 4)\n0x1000 0 4889e5 REX.W movq rbp,rsp\n0x1003 3 c3 ret\n'
                'Safepoints (size = 0)\n--- End code ---\n')
        evidence = capture.capture_target_code(text, 7, 'original')
        self.assertEqual(evidence['blocks'][0]['instructions'][0]['encoding_hex'], '4889e5')
        self.assertEqual(evidence['blocks'][0]['instructions'][0]['mnemonic'], 'movq')
        with self.assertRaises(ValueError):
            capture.capture_target_code(text.replace('4889e5', '4089e5'), 7, 'original')

    def test_capture_schema_mutations_fail(self):
        cases = [
            (['status'], 'complete'), (['gates_complete_before_timing'], True),
            (['trials'], [{'elapsed_s': 1}]), (['process_sets'], [{'id': 1}]),
            (['private_native_dump_retained'], False),
            (['native_gate', 'candidate', 'verified'], True),
            (['native_gate', 'candidate', 'dump_bytes'], source_native.MAX_NATIVE_TRACE_BYTES + 1),
            (['native_gate', 'candidate', 'semantic_verified'], True),
            (['native_gate', 'candidate', 'capture_verified'], False),
            (['native_gate', 'candidate', 'wasm_sha256'], 'f' * 64),
            (['native_gate', 'candidate', 'callback_function_index'], 102),
            (['native_gate', 'candidate', 'native_extraction_helper_sha256'], 'f' * 64),
            (['native_gate', 'candidate', 'engine', 'js_version'], 'different'),
            (['native_gate', 'candidate', 'checks', 0, 'raw_sha256'], 'f' * 64),
            (['native_gate', 'candidate', 'initialization', 'dispatch', 'worker_hardware_concurrency'], 8),
            (['activation', 'candidate', 'on', 'transformed_groups'], 0),
            (['browser_gate', 'candidate', 'checks', 0, 'alias', 0, 'callbacks'], 68),
            (['native_gate', 'candidate', 'code_evidence', 'semantic_verified'], True),
            (['native_gate', 'candidate', 'code_evidence', 'function_index'], 102),
            (['native_gate', 'candidate', 'code_evidence', 'blocks', 0, 'compiler'], 'unknown'),
            (['native_gate', 'candidate', 'code_evidence', 'blocks', 0, 'instructions', 0, 'offset'], 1),
            (['native_gate', 'candidate', 'code_evidence', 'blocks', 0, 'instructions', 0, 'encoding_hex'], 'private text'),
            (['native_gate', 'candidate', 'code_evidence', 'blocks', 0, 'instructions', 0, 'operands'], ['private_name']),
            (['native_gate', 'candidate', 'code_evidence', 'blocks', 0, 'encoding_sha256'], 'f' * 64),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                report = capture_fixture()
                current = report
                for key in path[:-1]:
                    current = current[key]
                current[path[-1]] = value
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    source_validate.validate(report, capture_only=True)

    def test_complete_code_reconstructs_from_bounded_stdout(self):
        evidence = capture_fixture()['native_gate']['candidate']['code_evidence']
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            capture.emit_target_code(evidence)
        lines = output.getvalue().splitlines()
        header = json.loads(lines[0].split(' ', 1)[1])
        expected_hash = header.pop('evidence_sha256')
        chunks = [json.loads(line.split(' ', 1)[1]) for line in lines[1:]]
        for block_index, block in enumerate(header['blocks']):
            group = [row for row in chunks if row['block'] == block_index]
            self.assertEqual([row['sequence'] for row in group], list(range(1, len(group) + 1)))
            self.assertTrue(all(row['total_chunks'] == len(group) for row in group))
            block['instructions'] = [row for chunk in group for row in chunk['instructions']]
        self.assertEqual(header, evidence)
        self.assertEqual(capture.canonical_hash(header), expected_hash)
        self.assertTrue(all(len(line.encode()) <= 8300 for line in lines))

    def test_invalid_evidence_emits_nothing(self):
        evidence = capture_fixture()['native_gate']['candidate']['code_evidence']
        evidence['blocks'][-1]['instructions'][-1]['offset'] += 1
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(ValueError):
                capture.emit_target_code(evidence)
        self.assertEqual(output.getvalue(), '')

    def test_unrecognized_target_text_rejected_outside_text_not_exported(self):
        text = native_fixture.dump(native_fixture.fixture())
        evidence = capture.capture_target_code('private browser stderr\n' + text, 8227, 'candidate')
        self.assertNotIn('private browser stderr', json.dumps(evidence))
        with self.assertRaises(ValueError):
            capture.capture_target_code(text.replace('Instructions (size = ', 'private target text\nInstructions (size = ', 1)
                                       .replace('Safepoints (size = 0)', 'unrecognized stream line\nSafepoints (size = 0)'), 8227, 'candidate')

    def test_capture_validator_cli_outputs_manual_review_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'capture.json'
            target = Path(folder) / 'capture-summary.json'
            source.write_text(json.dumps(capture_fixture()))
            output = io.StringIO()
            with patch.object(sys, 'argv', ['source_validate.py', '--capture-only', str(source), '--output', str(target)]), contextlib.redirect_stdout(output):
                source_validate.main()
            summary = json.loads(target.read_text())
            self.assertEqual(summary['status'], 'awaiting_manual_review')
            self.assertEqual(summary['primary_calls'], 0)
            self.assertFalse(summary['semantic_verified'])
            self.assertFalse(summary['future_primary_schedule_active'])
            self.assertIn('SOURCE_ON_NATIVE_CAPTURE_CODE ', output.getvalue())
            self.assertNotIn('SOURCE_CONFIRM_TRIAL ', output.getvalue())

    def test_non_capture_cli_is_blocked_before_artifact_or_browser_access(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('source_confirmation.py')),
                                 '--manifest', '/missing/manifest', '--harness', '/missing/harness',
                                 '--dispatch-helper', '/missing/helper', '--output', '/missing/output'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Primary timing is disabled', result.stderr)
        self.assertNotIn('FileNotFoundError', result.stderr)


if __name__ == '__main__':
    unittest.main()
