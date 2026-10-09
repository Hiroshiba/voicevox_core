"""Versioned source-bound JIT_CODE_LOAD extraction for manual review only.

Derived from the reviewed synthetic canary SHA256
4e46b8659d8aeda6778ff43f31ec9dc20fa007121312c2f0dcec72b20defd789.
Pinned V8 writer: 0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7.
The frozen V8-text/semantic parsers are unchanged and are not semantic gates here.
"""
import hashlib
import json
import re
import struct
import subprocess
import tempfile
from pathlib import Path

MAX_JIT_BYTES = 256 * 1024 * 1024
MAX_TARGET_BYTES = 65536
MAX_TARGET_RECORDS = 16
MAX_INSTRUCTIONS = 2400
TAIL_KINDS = {'truncated_record_header', 'truncated_record_body'}
FAILURE_KINDS = TAIL_KINDS | {'record_event_invalid', 'record_length_invalid', 'load_prefix_short',
    'load_binding_invalid', 'load_duplicate_id', 'load_name_terminator', 'load_code_length'}
RECORD_KINDS = ('code_load', 'code_move', 'debug_info', 'close', 'unwinding_info')
WASM_SYMBOL = re.compile(rb'JS:wasm-function\[[0-9]+\]-[0-9]+-(?:liftoff|turbofan)')

class ProbeError(ValueError):
    def __init__(self, category, details=None):
        super().__init__(category)
        self.details = details


def check(condition, category):
    if not condition:
        raise ProbeError(category)


def parse_jit(data, expected_pid, function_index, function_name):
    """Parse complete records in order; never resynchronize after an invalid tail.

    A partial final record is separately reported. Earlier full CODE_LOAD bytes
    retain their own complete size/symbol binding. No claim is made about absent
    later records, the entire file, or which tier executed the final call.
    """
    check(type(function_index) is int and 0 <= function_index <= 1000000, 'target_binding')
    check(function_name == f'wasm-function[{function_index}]', 'target_binding')
    target_pattern = re.compile(re.escape(f'JS:{function_name}-{function_index}-'.encode()) + rb'(liftoff|turbofan)')
    check(40 <= len(data) <= MAX_JIT_BYTES, 'jit_header')
    magic, version, size, machine, reserved, pid, timestamp, flags_value = struct.unpack_from('<6I2Q', data)
    check((magic, version, size, machine, reserved, pid, flags_value) ==
          (0x4A695444, 1, 40, 62, 0xDEADBEEF, expected_pid, 0), 'jit_header')
    offset, records, loads, other_wasm = 40, 0, 0, 0
    targets, ids = [], set()
    event, length = -1, 0

    def details(kind):
        return {'kind': kind, 'offset_bytes': offset, 'available_bytes': len(data) - offset,
                'declared_record_bytes': min(length, MAX_JIT_BYTES),
                'declared_record_exceeds_limit': length > MAX_JIT_BYTES,
                'record_kind': RECORD_KINDS[event] if 0 <= event < len(RECORD_KINDS) else 'unknown',
                'complete_records_before_failure': records}

    def invalid(condition, kind):
        if not condition:
            raise ProbeError('record_shape', details(kind))

    def finish(tail=None):
        return {'records': records, 'loads': loads, 'other_wasm': other_wasm, 'targets': targets,
                'validated_prefix_bytes': offset, 'trailing_bytes': len(data) - offset,
                'file_complete': tail is None, 'first_failure': tail}

    while offset < len(data):
        event, length = -1, 0
        if len(data) - offset < 16:
            return finish(details('truncated_record_header'))
        event, length, stamp = struct.unpack_from('<IIQ', data, offset)
        invalid(event in (0, 1, 2, 3, 4), 'record_event_invalid')
        invalid(16 <= length <= MAX_JIT_BYTES, 'record_length_invalid')
        if length > len(data) - offset:
            return finish(details('truncated_record_body'))
        record = data[offset:offset + length]
        if event == 0:
            invalid(length >= 58, 'load_prefix_short')
            rpid, tid, vma, address, code_size, code_id = struct.unpack_from('<II4Q', record, 16)
            invalid(rpid == pid and vma == address and address > 0 and code_size > 0, 'load_binding_invalid')
            invalid(code_id not in ids, 'load_duplicate_id')
            ids.add(code_id)
            end = record.find(b'\0', 56, min(length, 56 + 4097))
            invalid(end >= 56, 'load_name_terminator')
            invalid(length - end - 1 == code_size, 'load_code_length')
            name, code = record[56:end], record[end + 1:]
            match = target_pattern.fullmatch(name)
            if match:
                check(code_size <= MAX_TARGET_BYTES and len(targets) < MAX_TARGET_RECORDS, 'target_limit')
                targets.append({'tier': match.group(1).decode('ascii'), 'code': code, 'address': address})
            elif WASM_SYMBOL.fullmatch(name):
                other_wasm += 1
            loads += 1
        offset += length
        records += 1
    return finish()


PREFIXES = {'cs', 'ds', 'es', 'fs', 'gs', 'ss', 'data16', 'addr32', 'rep', 'repz', 'repnz', 'lock', 'bnd', 'rex.w'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def branch_target(code, offset):
    """Read relative x64 branch displacements from bytes, not display addresses."""
    p = 0
    while p < len(code) and code[p] in (0x66, 0x67, 0x2e, 0x3e, 0x26, 0x36, 0x64, 0x65, 0xf2, 0xf3): p += 1
    if p >= len(code): return None
    op = code[p]
    width = 1 if 0x70 <= op <= 0x7f or op == 0xeb else 4 if op in (0xe8, 0xe9) else 0
    if op == 0x0f and p + 1 < len(code) and 0x80 <= code[p + 1] <= 0x8f:
        width = 4
    if not width: return None
    check(len(code) >= width + 1, 'decoder_branch')
    return offset + len(code) + int.from_bytes(code[-width:], 'little', signed=True)


def decode_output(text, code):
    from source_native import safe_operand
    cursor = 0
    rows = []
    for line in text.splitlines():
        m = re.match(r'^\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s+)+)\s*(.*?)\s*$', line)
        if not m: continue
        offset, shown, assembly = m.groups()
        raw = bytes.fromhex(shown)
        check(int(offset, 16) == cursor and 0 < len(raw) <= 15 and assembly, 'decoder_coverage')
        check(raw == code[cursor:cursor + len(raw)], 'decoder_bytes')
        check('(bad)' not in assembly and not assembly.startswith('.byte'), 'decoder_unsupported')
        assembly = assembly.split('#', 1)[0].strip().lower()
        prefixes = []
        tokens = assembly.split(None, 1)
        while tokens[0] in PREFIXES:
            prefixes.append(tokens[0]); check(len(prefixes) <= 4 and len(tokens) == 2, 'decoder_prefix')
            tokens = tokens[1].split(None, 1)
        mnemonic = tokens[0]
        check(re.fullmatch(r'[a-z][a-z0-9]{0,19}', mnemonic) is not None, 'decoder_mnemonic')
        operands = [] if len(tokens) == 1 else [x.strip() for x in tokens[1].split(',')]
        check(len(operands) <= 8, 'decoder_operands')
        target = branch_target(raw, cursor)
        if target is not None:
            check(mnemonic.startswith('j') or mnemonic in ('call', 'callq'), 'decoder_branch')
            operands = [hex(target) if 0 <= target < len(code) else '<external>']
        else:
            # Absolute/address-derived objdump annotations are never published.
            operands = [re.sub(r'\s*<[^>]*>', '', value) for value in operands]
        operands = [safe_operand(value) for value in operands]
        rows.append({'offset': cursor, 'size': len(raw), 'prefixes': prefixes, 'mnemonic': mnemonic,
                     'operands': operands, 'target': target, 'encoding_hex': raw.hex()})
        cursor += len(raw)
    check(cursor == len(code) and 0 < len(rows) <= MAX_INSTRUCTIONS, 'decoder_coverage')
    return rows


def decode(code, scratch):
    path = scratch / 'target-code-private.bin'
    path.write_bytes(code)
    try:
        import os
        result = subprocess.run(['objdump', '-D', '-z', '-w', '--insn-width=16', '-M', 'intel',
             '-b', 'binary', '-m', 'i386:x86-64', str(path)], capture_output=True, timeout=10,
             env=dict(os.environ, LC_ALL='C'))
        check(result.returncode == 0 and len(result.stdout) <= 4 * 1024 * 1024, 'decoder_failed')
        return decode_output(result.stdout.decode('ascii', 'replace'), code)
    finally:
        path.unlink(missing_ok=True)


def module_name(wasm, index):
    """This source experiment previously printed fallback names. Verify that fact.

    A new/custom function name would require an explicit format review rather than
    silently broadening the public symbol grammar or weakening the binding.
    """
    import callback_discovery as cd
    for section in cd.sections(wasm):
        if section['id'] != 0: continue
        name, pos = cd.string(section['payload'], 0)
        if name != 'name': continue
        raw = section['payload']
        while pos < len(raw):
            kind = raw[pos]; length, start = cd.u(raw, pos + 1); end = start + length
            check(end <= len(raw), 'module_name_section')
            if kind == 1:
                count, at = cd.u(raw, start)
                for _ in range(count):
                    function, at = cd.u(raw, at); value, at = cd.string(raw, at)
                    check(function != index or not value, 'named_target_requires_review')
                check(at == end, 'module_name_section')
            pos = end
    return f'wasm-function[{index}]'


def parse_boundaries(text, index):
    """Private address joins to the source-defined executable instruction boundary."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r'^\s*\S+\s+pw:browser\s+\[pid=\d+\]\[(?:out|err)\]\s?', '', line)
        line = re.sub(r'^\s*\[pid=\d+\]\[(?:out|err)\]\s?', '', line)
        lines.append(line)
    text = '\n'.join(lines)
    raw_blocks = re.findall(r'--- WebAssembly code ---\s*(.*?)--- End code ---', text, re.S)
    check(0 < len(raw_blocks) <= MAX_TARGET_RECORDS, 'instruction_boundary_missing')
    result = []
    for raw in raw_blocks:
        def field(pattern):
            found = re.findall(pattern, raw, re.M)
            check(len(found) == 1, 'instruction_boundary_header')
            return found[0]
        named = field(r'^name:\s*wasm-function\[([0-9]+)\]\s*$')
        indexed = field(r'^index:\s*([0-9]+)\s*$')
        compiler = field(r'^compiler:\s*(Liftoff|TurboFan)\s*$')
        check(int(named) == int(indexed) == index, 'instruction_boundary_index')
        body, unpadded, padding = map(int, field(r'^Body \(size = ([0-9]+) = ([0-9]+) \+ ([0-9]+) padding\)\s*$'))
        check(body == unpadded + padding and 0 < unpadded <= body <= MAX_TARGET_BYTES, 'instruction_boundary_body')
        ranged = re.findall(r'^Instructions \(size = ([0-9]+), 0x([0-9a-fA-F]+)-0x([0-9a-fA-F]+)\)\s*$', raw, re.M)
        printed = None
        if ranged:
            check(len(ranged) == 1, 'instruction_boundary_header')
            amount, begin, end = ranged[0]; amount, begin, end = int(amount), int(begin, 16), int(end, 16)
            check(end - begin == amount, 'instruction_boundary_address_extent')
        else:
            amount = int(field(r'^Instructions \(size = ([0-9]+)\)\s*$'))
            first = re.search(r'^\s*0x([0-9a-fA-F]+)\s+0\s+[0-9a-fA-F]+\s+', raw, re.M)
            check(first is not None, 'instruction_boundary_start')
            begin = int(first[1], 16)
            from source_capture import extraction_module
            frozen = extraction_module().parse_blocks('--- WebAssembly code ---\n' + raw + '\n--- End code ---', index, include_encoding=True)
            check(len(frozen) == 1, 'instruction_boundary_text_blocks')
            printed = b''.join(bytes.fromhex(r['encoding_hex']) for r in frozen[0]['instructions'])
            check(len(printed) == amount, 'instruction_boundary_text_extent')
        check(0 < amount <= unpadded <= body and begin > 0, 'instruction_boundary_extent')
        result.append({'address': begin, 'compiler': compiler, 'allocation_bytes': body,
                      'unpadded_binary_bytes': unpadded, 'allocation_padding_bytes': padding,
                      'instruction_bytes': amount, 'printed_instruction_bytes': printed})
    return result


def summarize_files(directory, scratch, index, name, boundary_text):
    import stat
    paths = sorted(directory.iterdir())
    check(0 < len(paths) <= 16, 'jit_file_count')
    total = sum(p.lstat().st_size for p in paths)
    check(0 < total <= MAX_JIT_BYTES, 'jit_file_limit')
    summary = {'files': len(paths), 'readable_bytes': total, 'validated_prefix_bytes': 0,
        'trailing_bytes': 0, 'partial_files': 0, 'complete_records': 0, 'code_load_records': 0,
        'other_wasm_records': 0, 'target_files': 0, 'all_files_complete': True,
        'first_partial_tail': None, 'private_dump_sha256': None}
    boundaries = parse_boundaries(boundary_text, index)
    blocks = []
    hashes = []
    target_identities = {}
    for path in paths:
        check(stat.S_ISREG(path.lstat().st_mode) and re.fullmatch(r'jit-[0-9]+\.dump', path.name), 'jit_file_shape')
        data = path.read_bytes()
        hashes.append(digest(data))
        parsed = parse_jit(data, int(path.stem.split('-')[1]), index, name)
        summary['validated_prefix_bytes'] += parsed['validated_prefix_bytes']
        summary['trailing_bytes'] += parsed['trailing_bytes']
        summary['partial_files'] += int(not parsed['file_complete'])
        summary['all_files_complete'] &= parsed['file_complete']
        summary['complete_records'] += parsed['records']
        summary['code_load_records'] += parsed['loads']
        summary['other_wasm_records'] += parsed['other_wasm']
        summary['target_files'] += int(bool(parsed['targets']))
        if summary['first_partial_tail'] is None: summary['first_partial_tail'] = parsed['first_failure']
        for target in parsed['targets']:
            check(len(blocks) < MAX_TARGET_RECORDS, 'target_count')
            allocation = target['code']
            compiler = {'liftoff': 'Liftoff', 'turbofan': 'TurboFan'}[target['tier']]
            matches = [b for b in boundaries if b['address'] == target['address'] and b['compiler'] == compiler
                       and b['allocation_bytes'] == len(allocation)]
            check(len(matches) == 1, 'instruction_boundary_address_tier_binding')
            boundary = matches[0]
            identity = (target['address'], compiler, len(allocation))
            check(identity not in target_identities or target_identities[identity] == digest(allocation), 'reused_code_address_ambiguous')
            target_identities[identity] = digest(allocation)
            code = allocation[:boundary['instruction_bytes']]
            tail = allocation[len(code):]
            if boundary['printed_instruction_bytes'] is not None:
                check(boundary['printed_instruction_bytes'] == code, 'instruction_boundary_text_bytes')
            rows = decode(code, scratch)
            blocks.append({'compiler': compiler, 'instruction_bytes': len(code), 'encoding_sha256': digest(code),
                'decoder_bytes_sha256': digest(b''.join(bytes.fromhex(row['encoding_hex']) for row in rows)),
                'allocation_bytes': len(allocation), 'allocation_sha256': digest(allocation),
                'excluded_metadata_padding_bytes': len(tail), 'excluded_metadata_padding_sha256': digest(tail),
                'unpadded_binary_bytes': boundary['unpadded_binary_bytes'],
                'allocation_padding_bytes': boundary['allocation_padding_bytes'],
                'boundary_source': 'V8-WasmCode-Disassemble-header', 'private_address_tier_size_join_verified': True,
                'instructions': rows})
    summary['private_dump_sha256'] = digest(json.dumps(sorted(hashes), separators=(',', ':')).encode())
    check(summary['target_files'] == 1 and 0 < len(blocks) <= MAX_TARGET_RECORDS, 'target_process_binding')
    check(any(b['compiler'] == 'TurboFan' for b in blocks), 'optimized_target_missing')
    check(summary['validated_prefix_bytes'] + summary['trailing_bytes'] == summary['readable_bytes'], 'jit_coverage')
    return blocks, summary


def validate_evidence(value, pin, variant):
    from source_native import safe_operand
    check(set(value) == {'schema', 'variant', 'function_index', 'function_name', 'wasm_sha256',
        'callback_body_sha256', 'callback_table_slot', 'architecture', 'semantic_verified',
        'diagnostic_profiled', 'optimized_target_observed', 'decoder', 'blocks', 'jit_summary'}, 'evidence_schema')
    check(value['schema'] == 'inplace-leaky-jit-native-capture-v1' and value['variant'] == variant
        and value['architecture'] == 'x64' and value['semantic_verified'] is False
        and value['diagnostic_profiled'] is True and value['decoder'] == 'GNU-objdump-intel-x86-64-v1', 'evidence_scope')
    for key, source in [('function_index', 'callback_function_index'), ('wasm_sha256', 'wasm_sha256'),
                       ('callback_body_sha256', 'callback_body_sha256'), ('callback_table_slot', 'callback_table_slot')]:
        check(value[key] == pin[source], 'evidence_binding')
    check(value['function_name'] == f"wasm-function[{pin['callback_function_index']}]", 'evidence_name')
    check(variant in ('original', 'rebuilt', 'candidate') and type(value['function_index']) is int
          and 0 <= value['function_index'] <= 1000000 and type(value['callback_table_slot']) is int
          and 0 <= value['callback_table_slot'] <= 1000000, 'evidence_index')
    check(all(re.fullmatch(r'[0-9a-f]{64}', value[k]) is not None for k in ('wasm_sha256', 'callback_body_sha256')), 'evidence_module_digest')
    blocks = value['blocks']
    check(isinstance(blocks, list) and 0 < len(blocks) <= MAX_TARGET_RECORDS, 'evidence_blocks')
    check(value['optimized_target_observed'] is True and any(b['compiler'] == 'TurboFan' for b in blocks), 'evidence_tier')
    for block in blocks:
        check(set(block) == {'compiler', 'instruction_bytes', 'encoding_sha256', 'decoder_bytes_sha256', 'instructions',
            'allocation_bytes', 'allocation_sha256', 'excluded_metadata_padding_bytes', 'excluded_metadata_padding_sha256',
            'unpadded_binary_bytes', 'allocation_padding_bytes', 'boundary_source', 'private_address_tier_size_join_verified'}
              and block['compiler'] in ('Liftoff', 'TurboFan'), 'evidence_block')
        check(type(block['instruction_bytes']) is int and 0 < block['instruction_bytes'] <= MAX_TARGET_BYTES, 'evidence_size')
        check(block['boundary_source'] == 'V8-WasmCode-Disassemble-header'
              and block['private_address_tier_size_join_verified'] is True, 'evidence_boundary_source')
        for k in ('allocation_bytes', 'excluded_metadata_padding_bytes', 'unpadded_binary_bytes', 'allocation_padding_bytes'):
            check(type(block[k]) is int and 0 <= block[k] <= MAX_TARGET_BYTES, 'evidence_allocation_extent')
        check(block['allocation_bytes'] == block['instruction_bytes'] + block['excluded_metadata_padding_bytes']
              == block['unpadded_binary_bytes'] + block['allocation_padding_bytes']
              and block['instruction_bytes'] <= block['unpadded_binary_bytes'], 'evidence_instruction_boundary')
        for k in ('allocation_sha256', 'excluded_metadata_padding_sha256'):
            check(re.fullmatch(r'[0-9a-f]{64}', block[k]) is not None, 'evidence_allocation_digest')
        rows = block['instructions']; check(isinstance(rows, list) and 0 < len(rows) <= MAX_INSTRUCTIONS, 'evidence_rows')
        code = bytearray()
        for row in rows:
            check(set(row) == {'offset', 'size', 'prefixes', 'mnemonic', 'operands', 'target', 'encoding_hex'}, 'evidence_row')
            check(type(row['offset']) is int and row['offset'] == len(code) and type(row['size']) is int
                  and 0 < row['size'] <= 15, 'evidence_row_extent')
            check(isinstance(row['prefixes'], list) and len(row['prefixes']) <= 4 and all(x in PREFIXES for x in row['prefixes']), 'evidence_prefixes')
            check(re.fullmatch(r'[a-z][a-z0-9]{0,19}', row['mnemonic']) is not None, 'evidence_mnemonic')
            check(isinstance(row['operands'], list) and len(row['operands']) <= 8 and
                  all(x in ('<external>', '<unsupported>') or safe_operand(x) == x for x in row['operands']), 'evidence_operands')
            check(isinstance(row['encoding_hex'], str) and re.fullmatch(r'[0-9a-f]{2,30}', row['encoding_hex'])
                  and len(row['encoding_hex']) == row['size'] * 2, 'evidence_encoding')
            raw = bytes.fromhex(row['encoding_hex'])
            check(row['target'] == branch_target(raw, row['offset']), 'evidence_relative_branch')
            code.extend(raw)
        check(len(code) == block['instruction_bytes'] and digest(code) == block['encoding_sha256']
              == block['decoder_bytes_sha256'], 'evidence_code_hash')
        with tempfile.TemporaryDirectory(prefix='jit-evidence-redecode-') as temporary:
            check(decode(bytes(code), Path(temporary)) == rows, 'evidence_independent_decode')
    j = value['jit_summary']
    check(set(j) == {'files', 'readable_bytes', 'validated_prefix_bytes', 'trailing_bytes', 'partial_files',
        'complete_records', 'code_load_records', 'other_wasm_records', 'target_files', 'all_files_complete',
        'first_partial_tail', 'private_dump_sha256'}, 'jit_summary_schema')
    for key in set(j) - {'all_files_complete', 'first_partial_tail', 'private_dump_sha256'}:
        check(type(j[key]) is int and 0 <= j[key] <= MAX_JIT_BYTES, 'jit_summary_count')
    check(0 < j['files'] <= 16 and j['target_files'] == 1 and j['code_load_records'] > 0
          and j['validated_prefix_bytes'] + j['trailing_bytes'] == j['readable_bytes'], 'jit_summary_coverage')
    check(sum(b['instruction_bytes'] for b in blocks) <= j['validated_prefix_bytes'], 'jit_target_extent')
    check(re.fullmatch(r'[0-9a-f]{64}', j['private_dump_sha256']) is not None and type(j['all_files_complete']) is bool, 'jit_summary_digest')
    tail = j['first_partial_tail']
    if j['all_files_complete']:
        check(j['trailing_bytes'] == j['partial_files'] == 0 and tail is None, 'jit_complete_claim')
    else:
        check(0 < j['partial_files'] <= j['files'] and j['trailing_bytes'] > 0 and isinstance(tail, dict), 'jit_partial_claim')
        check(set(tail) == {'kind', 'offset_bytes', 'available_bytes', 'declared_record_bytes',
              'declared_record_exceeds_limit', 'record_kind', 'complete_records_before_failure'}, 'jit_tail_schema')
        check(tail['kind'] in TAIL_KINDS and tail['record_kind'] in (*RECORD_KINDS, 'unknown')
              and tail['declared_record_exceeds_limit'] is False, 'jit_tail_kind')
        for key in ('offset_bytes', 'available_bytes', 'declared_record_bytes', 'complete_records_before_failure'):
            check(type(tail[key]) is int and 0 <= tail[key] <= MAX_JIT_BYTES, 'jit_tail_count')
    return True


def make_evidence(directory, scratch, pin, variant, name, boundary_text):
    blocks, summary = summarize_files(directory, scratch, pin['callback_function_index'], name, boundary_text)
    value = {'schema': 'inplace-leaky-jit-native-capture-v1', 'variant': variant,
        'function_index': pin['callback_function_index'], 'function_name': name,
        'wasm_sha256': pin['wasm_sha256'], 'callback_body_sha256': pin['callback_body_sha256'],
        'callback_table_slot': pin['callback_table_slot'], 'architecture': 'x64', 'semantic_verified': False,
        'diagnostic_profiled': True, 'optimized_target_observed': True,
        'decoder': 'GNU-objdump-intel-x86-64-v1', 'blocks': blocks, 'jit_summary': summary}
    validate_evidence(value, pin, variant)
    return value
