"""Strict target-code transport for manual review; never a semantic acceptance gate."""
import hashlib
import json
from pathlib import Path
import re

from source_manifest import KEYS, need, sha

EXTRACTION_SHA256 = '366e7e3e32622f61797288b453d02e507f49f1f3f6fee2443c376fb9c87c65dd'
MAX_BLOCKS = 16
MAX_INSTRUCTIONS = 2400
MAX_INSTRUCTION_BYTES = 65536


def extraction_module():
    import source_native_capture_proof
    need(sha(Path(source_native_capture_proof.__file__).read_bytes()) == EXTRACTION_SHA256,
         'Reviewed capture extraction helper pin')
    return source_native_capture_proof


def canonical_hash(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def capture_target_code(text, function_index, variant):
    from source_native import safe_operand
    # This invokes only the previously reviewed byte-coverage lexer. The semantic
    # analyzer in the isolated helper is deliberately never called by this path.
    blocks = extraction_module().parse_blocks(text, function_index, include_encoding=True)
    need(1 <= len(blocks) <= MAX_BLOCKS, 'Target native block count outside capture bound')
    for block in blocks:
        for row in block['instructions']:
            row['operands'] = [safe_operand(value) for value in row['operands']]
        code = b''.join(bytes.fromhex(row['encoding_hex']) for row in block['instructions'])
        block['encoding_sha256'] = sha(code)
    evidence = {'schema': 'inplace-leaky-native-code-capture-v1', 'variant': variant,
                'function_index': function_index, 'architecture': 'x64',
                'semantic_verified': False, 'extraction_helper_sha256': EXTRACTION_SHA256,
                'optimized_target_observed': any(block['compiler'] == 'TurboFan' for block in blocks),
                'blocks': blocks}
    validate_target_code(evidence, function_index, variant)
    return evidence


def validate_target_code(evidence, function_index, variant):
    from source_native import safe_operand
    need(set(evidence) == {'schema', 'variant', 'function_index', 'architecture', 'semantic_verified',
                           'extraction_helper_sha256', 'optimized_target_observed', 'blocks'}, 'Capture evidence schema')
    need(evidence['schema'] == 'inplace-leaky-native-code-capture-v1' and variant in KEYS
         and evidence['variant'] == variant and type(function_index) is int and function_index >= 0
         and evidence['function_index'] == function_index and evidence['architecture'] == 'x64'
         and evidence['semantic_verified'] is False and evidence['extraction_helper_sha256'] == EXTRACTION_SHA256,
         'Capture evidence identity; semantic acceptance is forbidden')
    blocks = evidence['blocks']
    need(isinstance(blocks, list) and 1 <= len(blocks) <= MAX_BLOCKS, 'Capture block bound')
    need(type(evidence['optimized_target_observed']) is bool
         and evidence['optimized_target_observed'] == any(block['compiler'] == 'TurboFan' for block in blocks),
         'Observed native tier association')
    for block in blocks:
        need(set(block) == {'compiler', 'instruction_bytes', 'instructions', 'encoding_sha256'}
             and block['compiler'] in {'Liftoff', 'TurboFan'}, 'Captured block schema')
        need(type(block['instruction_bytes']) is int and 1 <= block['instruction_bytes'] <= MAX_INSTRUCTION_BYTES,
             'Captured instruction byte bound')
        rows = block['instructions']
        need(isinstance(rows, list) and 1 <= len(rows) <= MAX_INSTRUCTIONS, 'Captured instruction count')
        cursor = 0
        code = bytearray()
        for row in rows:
            need(set(row) == {'offset', 'size', 'mnemonic', 'operands', 'target', 'encoding_hex'}, 'Captured instruction schema')
            need(type(row['offset']) is int and row['offset'] == cursor
                 and type(row['size']) is int and 1 <= row['size'] <= 15, 'Complete contiguous instruction coverage')
            need(isinstance(row['encoding_hex'], str) and re.fullmatch('[0-9a-f]{2,30}', row['encoding_hex']) is not None
                 and len(row['encoding_hex']) == 2 * row['size'], 'Exact captured instruction encoding')
            need(isinstance(row['mnemonic'], str) and re.fullmatch('[a-z][a-z0-9]{0,19}', row['mnemonic']) is not None,
                 'Captured mnemonic grammar')
            need(isinstance(row['operands'], list) and len(row['operands']) <= 8, 'Captured operand bound')
            for operand in row['operands']:
                need(isinstance(operand, str) and (operand in ['<external>', '<unsupported>'] or safe_operand(operand) == operand),
                     'Unsafe captured operand text')
            need(row['target'] is None or type(row['target']) is int and -(2 ** 31) <= row['target'] < 2 ** 31,
                 'Captured relative branch target')
            code.extend(bytes.fromhex(row['encoding_hex']))
            cursor += row['size']
        need(cursor == block['instruction_bytes'] and sha(bytes(code)) == block['encoding_sha256'],
             'Captured whole-code coverage/hash')
    return True


def emit_target_code(evidence):
    """Validated, numbered chunks recover the complete target code from CI logs."""
    validate_target_code(evidence, evidence['function_index'], evidence['variant'])
    evidence_sha = canonical_hash(evidence)
    header = {key: value for key, value in evidence.items() if key != 'blocks'}
    header.update(evidence_sha256=evidence_sha, blocks=[{key: value for key, value in block.items() if key != 'instructions'}
                                                     for block in evidence['blocks']])
    print('SOURCE_ON_NATIVE_CAPTURE_META ' + json.dumps(header, separators=(',', ':')), flush=True)
    for block_index, block in enumerate(evidence['blocks']):
        rows = block['instructions']
        chunks = [rows[index:index + 8] for index in range(0, len(rows), 8)]
        for sequence, chunk in enumerate(chunks, 1):
            value = {'variant': evidence['variant'], 'function_index': evidence['function_index'],
                     'evidence_sha256': evidence_sha, 'block': block_index, 'sequence': sequence,
                     'total_chunks': len(chunks), 'instructions': chunk}
            encoded = json.dumps(value, separators=(',', ':'))
            need(len(encoded.encode()) <= 8192, 'Capture stdout chunk exceeds bound')
            print('SOURCE_ON_NATIVE_CAPTURE_CODE ' + encoded, flush=True)
