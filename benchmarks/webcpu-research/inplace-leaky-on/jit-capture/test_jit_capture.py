"""Synthetic-only contracts for source binding, native bytes and capture scope."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'browser'))
import jit_native as native
import jit_transport as t
import jit_validate as validator
import source_capture
from source_manifest import KEYS
from test_browser import fixture as old_fixture

CODE = bytes.fromhex('554889e5f30f59c05dc3')
PID = 123


def binding(wasm_sha, size=41):
    return {'schema': 'inplace-jit-module-binding-v1', 'wasm_sha256': wasm_sha, 'byte_length': size,
        'compilation_calls': 1, 'instantiation_calls': 1, 'tracked_modules': 1, 'workers_created': 2,
        'known_module_transfers': 2, 'violations': 0, 'all_compilation_bytes_exact': True,
        'all_worker_urls_exact': True, 'module_transfer_verified': True}


def code_load(index, code=CODE, tier='turbofan', ident=0):
    name = f'JS:wasm-function[{index}]-{index}-{tier}'.encode()
    return struct.pack('<IIQII4Q', 0, 56 + len(name) + 1 + len(code), 1, PID, PID, 0x1000, 0x1000,
                       len(code), ident) + name + b'\0' + code


def dump(index, suffix=b''):
    return struct.pack('<6I2Q', 0x4A695444, 1, 40, 62, 0xDEADBEEF, PID, 0, 0) + code_load(index) + suffix


def boundary(index, instruction_size=len(CODE), allocation_size=len(CODE), address=0x1000, compiler='TurboFan'):
    return (f'--- WebAssembly code ---\nname: wasm-function[{index}]\nindex: {index}\nkind: wasm function\ncompiler: {compiler}\n'
            f'Body (size = {allocation_size} = {allocation_size} + 0 padding)\n'
            f'Instructions (size = {instruction_size}, 0x{address:x}-0x{address+instruction_size:x})\n--- End code ---\n')


def evidence(pin, variant='original', partial=False):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); jit = root/'jit'; jit.mkdir()
        (jit/f'jit-{PID}.dump').write_bytes(dump(pin['callback_function_index'], b'abc' if partial else b''))
        return t.make_evidence(jit, root, pin, variant, f"wasm-function[{pin['callback_function_index']}]", boundary(pin['callback_function_index']))


def fixture():
    report = old_fixture()
    report.update(schema='inplace-leaky-on-jit-capture-v1', capture_only=True, status='awaiting_manual_review',
        stage='awaiting_manual_review', gates_complete_before_timing=False, trials=[], process_sets=[],
        private_temporary_directory_deleted=True, private_native_dump_retained=False,
        native_capture_backend='profiled_jitdump_module_audit_v1',
        jit_capture_source_hashes={p.name: native.sha(p.read_bytes()) for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py', '.js')})
    for variant, row in report['native_gate'].items():
        for k in ('proof', 'native_proof_helper_sha256', 'dump_sha256', 'dump_bytes'): row.pop(k)
        row.update(schema='inplace-leaky-browser-jit-capture-v1', verified=False, diagnostic_profiled=True,
            uninstrumented_code_identity_claimed=False, wasm_bytes=41, capture_verified=True,
            semantic_verified=False, source_pins=native.source_pins(), private_jit_deleted=True,
            stream_diagnostics={'stdout_bytes': 0, 'stderr_bytes': 10, 'sha256': 'a'*64, 'flag_errors': 0, 'permission_errors': 0},
            launch_flags=native.diagnostic_flags(row['callback_function_index']))
        b = binding(row['wasm_sha256'])
        row['initialization'].update(timing_instrumentation='native_module_audit', native_module_binding=b)
        row['drain'] = {'functions': 256, 'calls': 262144, 'result_verified': True, 'module_binding': copy.deepcopy(b)}
        row['code_evidence'] = evidence(report['provenance'][variant], variant, partial=True)
    def engine_fix(x):
        if isinstance(x, dict):
            if x.get('product') == 'synthetic Chromium': x.update(product='HeadlessChrome/153.0.8010.12', js_version='15.3.76.4')
            for v in x.values(): engine_fix(v)
        elif isinstance(x, list):
            for v in x: engine_fix(v)
    engine_fix(report)
    return report


class JitCaptureTests(unittest.TestCase):
    def test_full_fixture_is_capture_only_and_explicitly_profiled(self):
        report = fixture(); self.assertTrue(validator.validate(report))
        self.assertEqual(report['trials'], []); self.assertEqual(report['process_sets'], [])
        with self.assertRaises(ValueError): validator.validate(report, capture_only=False)
        for row in report['native_gate'].values():
            self.assertFalse(row['verified']); self.assertFalse(row['semantic_verified'])
            self.assertTrue(row['private_jit_deleted'])

    def test_synthetic_byte_payload_and_intel_decoder(self):
        report = fixture(); row = report['native_gate']['original']['code_evidence']['blocks'][0]
        self.assertEqual(row['instruction_bytes'], len(CODE))
        self.assertEqual(row['encoding_sha256'], hashlib.sha256(CODE).hexdigest())
        self.assertEqual(bytes.fromhex(''.join(x['encoding_hex'] for x in row['instructions'])), CODE)
        self.assertIn('mulss', [x['mnemonic'] for x in row['instructions']])

    def test_branch_target_is_decoded_from_encoding_not_address_text(self):
        self.assertEqual(t.branch_target(bytes.fromhex('75fc'), 10), 8)
        self.assertEqual(t.branch_target(bytes.fromhex('0f8501000000'), 10), 17)
        self.assertEqual(t.branch_target(bytes.fromhex('e8fbffffff'), 10), 10)
        self.assertIsNone(t.branch_target(bytes.fromhex('ffd0'), 10))
        code = bytes.fromhex('75fec3')
        with tempfile.TemporaryDirectory() as d: rows = t.decode(code, Path(d))
        self.assertEqual(rows[0]['target'], 0)

    def test_frozen_v8_extractor_matches_same_bytes(self):
        dump_text = ('--- WebAssembly code ---\nname: wasm-function[101]\nindex: 101\nkind: wasm function\ncompiler: TurboFan\n'
            'Instructions (size = 4)\n0x1000 0 4889e5 REX.W movq rbp,rsp\n0x1003 3 c3 ret\n--- End code ---\n')
        old = source_capture.capture_target_code(dump_text, 101, 'original')
        code = bytes.fromhex('4889e5c3')
        with tempfile.TemporaryDirectory() as d: rows = t.decode(code, Path(d))
        self.assertEqual(old['blocks'][0]['encoding_sha256'], hashlib.sha256(code).hexdigest())
        self.assertEqual(bytes.fromhex(''.join(x['encoding_hex'] for x in rows)), code)
        self.assertEqual([x['offset'] for x in old['blocks'][0]['instructions']], [x['offset'] for x in rows])

    def test_wrong_index_symbol_and_missing_optimized_record_fail(self):
        p = fixture()['provenance']['original']; i = p['callback_function_index']
        parsed = t.parse_jit(dump(i), PID, i+1, f'wasm-function[{i+1}]')
        self.assertEqual(parsed['targets'], [])
        with self.assertRaises(t.ProbeError): t.parse_jit(dump(i), PID, i, 'private_name')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);j=root/'jit';j.mkdir()
            data = dump(i).replace(b'turbofan', b'liftoff')
            # Build the correct shorter record rather than mutating its length.
            data = struct.pack('<6I2Q',0x4A695444,1,40,62,0xDEADBEEF,PID,0,0)+code_load(i,tier='liftoff')
            (j/f'jit-{PID}.dump').write_bytes(data)
            with self.assertRaises(t.ProbeError): t.make_evidence(j,root,p,'original',f'wasm-function[{i}]',boundary(i,compiler='Liftoff'))

    def test_partial_target_never_accepted_and_malformed_tail_never_resyncs(self):
        p = t.parse_jit(dump(1)[:-1], PID, 1, 'wasm-function[1]')
        self.assertEqual(p['targets'], []); self.assertFalse(p['file_complete'])
        with self.assertRaises(t.ProbeError): t.parse_jit(dump(1, struct.pack('<IIQ',99,16,0)+code_load(1,ident=1)), PID,1,'wasm-function[1]')

    def test_allocation_metadata_is_excluded_by_v8_header_not_guessing(self):
        pin=fixture()['provenance']['original'];i=pin['callback_function_index'];metadata=b'\x00\x04\x00\x00\xcc'
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);j=root/'jit';j.mkdir()
            data=struct.pack('<6I2Q',0x4A695444,1,40,62,0xDEADBEEF,PID,0,0)+code_load(i,code=CODE+metadata)
            (j/f'jit-{PID}.dump').write_bytes(data)
            e=t.make_evidence(j,root,pin,'original',f'wasm-function[{i}]',boundary(i,len(CODE),len(CODE+metadata)))
            b=e['blocks'][0]
            self.assertEqual(b['instruction_bytes'],len(CODE));self.assertEqual(b['allocation_bytes'],len(CODE+metadata))
            self.assertEqual(b['excluded_metadata_padding_bytes'],len(metadata))
            self.assertEqual(b['excluded_metadata_padding_sha256'],hashlib.sha256(metadata).hexdigest())
            self.assertEqual(b['encoding_sha256'],hashlib.sha256(CODE).hexdigest())
            for text in ('', boundary(i,len(CODE),len(CODE+metadata),address=0x2000),
                         boundary(i,len(CODE),len(CODE+metadata),compiler='Liftoff'),
                         boundary(i,len(CODE),len(CODE+metadata))+boundary(i,len(CODE),len(CODE+metadata))):
                with self.assertRaises(t.ProbeError):t.make_evidence(j,root,pin,'original',f'wasm-function[{i}]',text)
            self.assertNotIn('0x1000',json.dumps(e))

    def test_module_name_custom_metadata_fails_closed(self):
        import callback_discovery as cd
        wasm = bytes.fromhex('0061736d0100000001060160017d017d03020100070501016600000a0c010a002000430000003f940b')
        self.assertEqual(t.module_name(wasm,0),'wasm-function[0]')
        payload = b'\x04name' + b'\x01\x04\x01\x00\x01x'
        with self.assertRaises(t.ProbeError): t.module_name(wasm+cd.section(0,payload),0)

    def test_binding_wrong_bytes_compilations_transfers_and_workers_fail(self):
        good = binding('a'*64); native.validate_binding(good,'a'*64,41)
        for key,value in [('wasm_sha256','b'*64),('byte_length',40),('compilation_calls',2),('instantiation_calls',2),
                ('tracked_modules',2),('workers_created',3),('known_module_transfers',1),('violations',1),
                ('all_compilation_bytes_exact',False),('all_worker_urls_exact',False),('module_transfer_verified',False)]:
            b=copy.deepcopy(good);b[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): native.validate_binding(b,'a'*64,41)

    def test_worker_transform_is_exact_and_no_primary_worker_change(self):
        before=(ROOT.parent/'browser/source_worker.js').read_text();after=native.audit_worker(before)
        self.assertEqual(native.sha(before.encode()),'3be1e45a9ea9522f950c82a91cb553e6103d8f04df36778366d464d560f1b64b')
        self.assertIn('native_module_audit',after);self.assertIn('native_drain',after)
        self.assertEqual((ROOT.parent/'browser/source_worker.js').read_text(),before)
        with self.assertRaises(ValueError):native.audit_worker(before+' ')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'worker.js';p.write_text(after)
            self.assertEqual(subprocess.run(['node','--check',str(p)],capture_output=True).returncode,0)

    def test_report_mutations_do_not_release_timings_or_private_fields(self):
        original = fixture()
        mutations = [(['status'],'complete'),(['gates_complete_before_timing'],True),(['trials'],[{}]),
            (['process_sets'],[{}]),(['private_temporary_directory_deleted'],False),(['private_native_dump_retained'],True),
            (['native_capture_backend'],'old'),(['native_gate','original','semantic_verified'],True),
            (['native_gate','original','verified'],True),(['native_gate','original','capture_verified'],False),
            (['native_gate','original','diagnostic_profiled'],False),(['native_gate','original','uninstrumented_code_identity_claimed'],True),
            (['native_gate','original','private_jit_deleted'],False),(['native_gate','original','launch_flags'],['--no-liftoff']),
            (['native_gate','original','wasm_sha256'],'b'*64),(['native_gate','original','stream_diagnostics','permission_errors'],1),
            (['native_gate','original','drain','calls'],1),(['native_gate','original','drain','module_binding','compilation_calls'],2),
            (['native_gate','original','checks',0,'raw_sha256'],'b'*64),(['activation','original','on','transformed_groups'],0),
            (['native_gate','original','raw_stderr'],'PRIVATE'),(['native_gate','original','code_evidence','blocks',0,'raw_asm'],'PRIVATE'),
            (['native_gate','original','code_evidence','jit_summary','all_files_complete'],True),
            (['native_gate','original','code_evidence','function_index'],100),
            (['native_gate','original','code_evidence','blocks',0,'instruction_bytes'],1),
            (['native_gate','original','code_evidence','blocks',0,'allocation_bytes'],1),
            (['native_gate','original','code_evidence','blocks',0,'private_address_tier_size_join_verified'],False),
            (['native_gate','original','code_evidence','blocks',0,'boundary_source'],'guessed_ret_boundary'),
            (['native_gate','original','code_evidence','blocks',0,'instructions',0,'offset'],1),
            (['native_gate','original','code_evidence','blocks',0,'instructions',0,'mnemonic'],'ret'),
            (['native_gate','original','code_evidence','blocks',0,'instructions',0,'operands'],['private_symbol']),
            (['native_gate','original','code_evidence','blocks',0,'encoding_sha256'],'b'*64)]
        for path,value in mutations:
            r=copy.deepcopy(original);loc=r
            for key in path[:-1]:loc=loc[key]
            loc[path[-1]]=value
            with self.subTest(path=path),self.assertRaises((ValueError,KeyError,TypeError)):validator.validate(r)

    def test_no_timing_loop_in_new_driver_and_default_cli_blocks(self):
        driver=ROOT/'jit_confirmation.py';text=driver.read_text()
        self.assertNotIn("stage('primary_set_'",text);self.assertNotIn("report['trials'].append",text)
        process=subprocess.run([sys.executable,str(driver),'--manifest','/missing','--harness','/missing','--dispatch-helper','/missing','--output','/missing'],capture_output=True,text=True)
        self.assertEqual(process.returncode,2);self.assertIn('Primary timing is disabled',process.stderr)

    def test_evidence_chunks_reassemble_all_bytes(self):
        e=fixture()['native_gate']['original']['code_evidence'];output=io.StringIO()
        with contextlib.redirect_stdout(output):validator.emit_evidence(e)
        lines=output.getvalue().splitlines();header=json.loads(lines[0].split(' ',1)[1]);expected=header.pop('evidence_sha256')
        chunks=[json.loads(l.split(' ',1)[1]) for l in lines[1:]]
        for i,b in enumerate(header['blocks']):b['instructions']=[r for c in chunks if c['block']==i for r in c['instructions']]
        self.assertEqual(header,e);self.assertEqual(validator.canonical_hash(header),expected)
        self.assertTrue(all(len(l.encode())<=8300 for l in lines))

    def test_invalid_evidence_never_emits_log_chunks(self):
        value=fixture()['native_gate']['original']['code_evidence']
        value['blocks'][0]['instructions'][0]['mnemonic']='private'
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaises(ValueError):validator.emit_evidence(value)
        self.assertEqual(output.getvalue(),'')

    def test_private_bounds_reject_symlink_and_total_overflow(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'jit').mkdir();(p/'regular').write_bytes(b'abc');native.private_bounds(p)
            (p/'link').symlink_to(p/'regular')
            with self.assertRaises(ValueError):native.private_bounds(p)
            (p/'link').unlink()
            with patch.object(native,'MAX_JIT_BYTES',2),self.assertRaises(ValueError):native.private_bounds(p)


if __name__ == '__main__':unittest.main()
