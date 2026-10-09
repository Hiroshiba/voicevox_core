"""Fail-closed source/IR/archive verification for the pinned activation study."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path

TARGETS = {
    '_ZNK11onnxruntime8functors9LeakyReluIfEclEll',
    '_ZNSt3__210__function6__funcIN11onnxruntime8functors9LeakyReluIfEENS_9allocatorIS5_EEFvllEEclEOlSA_',
}

def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def functions(text):
    result = {}
    for match in re.finditer(r'^define .*?^}', text, re.M | re.S):
        name = re.search(r'@([^\s(]+)\(', match[0])[1]
        require(name not in result, 'Duplicate IR function definition')
        result[name] = match[0]
    return result


def discover_source_root(text, suffix):
    match = re.search(r'^source_filename = "([^"\n]+)"$', text, re.M)
    require(match and match[1].endswith('/' + suffix), 'Unexpected IR source filename')
    return match[1][:-len(suffix)-1]


def normalize_ir(text, root):
    """Only ModuleID, source filename prefix and exact diagnostic filename arrays.

    No instructions, attributes, metadata, SSA names or symbols are normalized.
    The original array length is checked before changing a diagnostic filename.
    """
    require(root and '\\' not in root and '"' not in root, 'Unsupported source root spelling')
    lines, changes = [], []
    for line in text.splitlines(True):
        before = line
        if line.startswith('; ModuleID = '):
            require(re.fullmatch(r"; ModuleID = '[^\n]*'\n", line), 'Unexpected ModuleID')
            line = "; ModuleID = 'unmodified-control'\n"
        elif line.startswith('source_filename = '):
            require(line.startswith('source_filename = "' + root + '/'), 'Unexpected source filename')
            line = line.replace(root, 'ORT_SOURCE', 1)
        elif root in line:
            m = re.fullmatch(r'(@[^ ]+ = private unnamed_addr constant )\[(\d+) x i8\] c"([^"\n]+)"(, align \d+\n)', line)
            require(m and m[3].startswith(root + '/') and m[3].endswith('\\00'), 'Source path outside diagnostic filename constant')
            require('\\' not in m[3][:-3], 'Escaped diagnostic paths need separate review')
            require(int(m[2]) == len(m[3][:-3].encode()) + 1, 'Incorrect original diagnostic array length')
            payload = m[3].replace(root, 'ORT_SOURCE', 1)
            line = m[1] + f'[{len(payload[:-3].encode()) + 1} x i8] c"' + payload + '"' + m[4]
        if line != before:
            changes.append({'kind': 'ModuleID' if before.startswith('; ModuleID') else 'source_filename' if before.startswith('source_filename') else 'diagnostic_filename', 'before_sha256': sha(before.encode()), 'after_sha256': sha(line.encode())})
        lines.append(line)
    require(len(changes) == 9, 'Expected precisely one ModuleID, one source filename, seven diagnostic filename normalizations')
    return ''.join(lines), changes


def compare_control(original, rebuilt, pins):
    fa, fb = functions(original), functions(rebuilt)
    require(fa == fb and len(fa) == pins['function_definition_count'], 'Original control function IR mismatch')
    a, ac = normalize_ir(original, discover_source_root(original, pins['source_filename_suffix']))
    b, bc = normalize_ir(rebuilt, discover_source_root(rebuilt, pins['source_filename_suffix']))
    require(a == b, 'Original control differs beyond allowed diagnostic paths')
    require(sha(a.encode()) == pins['original_normalized_ir_sha256'], 'Original normalized IR pin mismatch')
    return {'passed': True, 'function_definition_count': len(fa), 'all_function_definitions_exact_IR_equal_without_normalization': True, 'complete_IR_equal_after_declared_normalization': True, 'normalized_IR_sha256': sha(a.encode()), 'normalizations': {'original': ac, 'rebuilt': bc}}


def compare_candidate(rebuilt, candidate, pins):
    fa, fb = functions(rebuilt), functions(candidate)
    require(fa.keys() == fb.keys() and len(fa) == pins['function_definition_count'], 'Candidate function set changed')
    require(TARGETS <= fa.keys(), 'Expected LeakyRelu definitions missing')
    ma = dict(re.findall(r'^!(\d+) = (.*)$', rebuilt, re.M))
    mb = dict(re.findall(r'^!(\d+) = (.*)$', candidate, re.M))
    mapping, inverse = {}, {}
    def pair_text(a, b):
        pa, pb = re.split(r'!(\d+)\b', a), re.split(r'!(\d+)\b', b)
        require(pa[::2] == pb[::2], 'Unexpected candidate instruction or metadata content difference')
        for i in range(1, len(pa), 2):
            pair_node(pa[i], pb[i])
    def pair_node(x, y):
        if x in mapping:
            require(mapping[x] == y, 'Nonfunctional metadata mapping')
            return
        require(y not in inverse, 'Nonbijective metadata mapping')
        require(x in ma and y in mb, 'Unresolved metadata edge')
        mapping[x], inverse[y] = y, x
        pair_text(ma[x], mb[y])
    counts, diagnostics = {}, []
    for name, old in fa.items():
        new = fb[name]
        if old == new:
            pair_text(old, new)
            label = 'raw_IR_identical'
        elif name in TARGETS:
            label = 'intended_LeakyRelu_exact_alias_change'
        else:
            expected = (51, 56) if 'HardSigmoidIfE4Init' in name else (222, 249) if 'SeluIfE4Init' in name else None
            if expected:
                def diagnostic(s, number):
                    out, count = [], 0
                    for line in s.splitlines(True):
                        if '@_ZN11onnxruntime15LogRuntimeError' in line:
                            require(line.rstrip().endswith(f', i32 noundef {number})'), 'Unexpected diagnostic line operand')
                            line = re.sub(r', i32 noundef ' + str(number) + r'\)(\n?)$', r', i32 noundef DIAGNOSTIC_LINE)\1', line)
                            count += 1
                        out.append(line)
                    require(count == 2, 'Expected two diagnostic calls')
                    return ''.join(out)
                old, new = diagnostic(old, expected[0]), diagnostic(new, expected[1])
                diagnostics.append({'function': name, 'old_line': expected[0], 'new_line': expected[1], 'calls': 2})
            pair_text(old, new)
            label = 'diagnostic_line_only' if expected else 'metadata_ID_renumbering_only'
        counts[label] = counts.get(label, 0) + 1
    attrs_a = dict(re.findall(r'^attributes #(\d+) = (.*)$', rebuilt, re.M))
    attrs_b = dict(re.findall(r'^attributes #(\d+) = (.*)$', candidate, re.M))
    require(attrs_a == attrs_b, 'Candidate attributes changed')
    require(counts == pins['candidate_scope_counts'], 'Candidate scope counts changed')
    normalized, changes = normalize_ir(candidate, discover_source_root(candidate, pins['source_filename_suffix']))
    # Pin the complete module too, including all changed bodies, globals and metadata
    # additions. The graph proof is not permission to ignore anything else.
    require(sha(normalized.encode()) == pins['candidate_normalized_ir_sha256'], 'Candidate complete normalized IR pin mismatch')
    return {'passed': True, 'definition_count': len(fa), 'counts': counts, 'metadata_nodes_matched': len(mapping), 'metadata_mapping_bijective': True, 'metadata_edges_recursively_equal': True, 'original_attribute_definitions_identical': True, 'diagnostic_line_changes': diagnostics, 'normalized_IR_sha256': sha(normalized.encode()), 'normalizations': changes}


def archive_entries(data):
    """Ordered GNU ar members, retaining duplicate names and original byte records."""
    require(data[:8] == b'!<arch>\n', 'Not a regular GNU ar archive')
    pos, names, index = 8, b'', 0
    while pos < len(data):
        header = data[pos:pos+60]
        require(len(header) == 60 and header[58:] == b'`\n', 'Malformed archive header')
        try:
            size = int(header[48:58])
        except ValueError as error:
            raise ValueError('Invalid archive member length') from error
        require(size >= 0, 'Negative archive member size')
        end = pos + 60 + size
        require(end + (size & 1) <= len(data), 'Truncated archive member')
        payload = data[pos+60:end]
        raw = data[pos:end+(size & 1)]
        pos += len(raw)
        name = header[:16].decode('ascii').strip()
        member_index = None
        if name == '//':
            require(not names, 'Multiple archive long-name tables')
            names = payload
        elif name not in ('/', '/SYM64/'):
            if name.startswith('/'):
                require(name[1:].isdigit(), 'Unknown archive metadata member')
                offset = int(name[1:])
                require(offset < len(names) and (offset == 0 or names[offset-2:offset] == b'/\n'), 'Invalid long-name table offset')
                require(b'/\n' in names[offset:], 'Unterminated long archive name')
                name = names[offset:].split(b'/\n', 1)[0].decode()
            else:
                require(not name.startswith('#1/'), 'BSD archive requires explicit support')
                name = name.rstrip('/')
            require(bool(name), 'Empty archive member name')
            index += 1
            member_index = index
        yield name, header, payload, raw, member_index
    require(pos == len(data), 'Trailing archive bytes')


def archive_manifest(data):
    return [{'index': index, 'name': name, 'sha256': sha(payload)} for name, _, payload, _, index in archive_entries(data) if index]


def replace_activation(data, replacement, pins):
    require(sha(data) == pins['pristine_runtime_archive_sha256'], 'Pristine source archive pin mismatch')
    before = archive_manifest(data)
    require(len(before) == pins['archive_member_count'], 'Archive member count changed')
    activations = [r for r in before if r['name'] == 'activations.cc.o']
    require(len(activations) == 2 and activations[0]['index'] == pins['activation_member_index'], 'Ambiguous CPU activation member')
    require(activations[0]['sha256'] == pins['original_activation_object_sha256'] and activations[1]['sha256'] == pins['duplicate_activation_object_sha256'], 'Activation occurrence hashes changed')
    output, changed = [b'!<arch>\n'], 0
    for name, header, payload, raw, index in archive_entries(data):
        if name in ('/', '/SYM64/'):
            continue  # The caller must regenerate the symbol index with llvm-ar s.
        if index == pins['activation_member_index']:
            require(name == 'activations.cc.o', 'Wrong replacement member')
            require(len(str(len(replacement))) <= 10, 'Replacement member too large')
            output.extend([header[:48] + str(len(replacement)).ljust(10).encode() + header[58:], replacement, b'\n' if len(replacement) & 1 else b''])
            changed += 1
        else:
            output.append(raw)
    require(changed == 1, 'Expected one CPU activation replacement')
    return b''.join(output)


def verify_archive_replacement(before, after, object_sha, pins):
    a, b = archive_manifest(before), archive_manifest(after)
    require(len(a) == len(b) == pins['archive_member_count'], 'Archive member count mismatch')
    changes = [(x, y) for x, y in zip(a, b) if x != y]
    require(len(changes) == 1, 'Archive changes outside one member')
    old, new = changes[0]
    require(old['index'] == new['index'] == pins['activation_member_index'] and old['name'] == new['name'] == 'activations.cc.o' and new['sha256'] == object_sha, 'Wrong archive replacement')
    return {'members': len(a), 'changed_members': changes, 'all_other_ordered_member_payloads_identical': True}


def verify_native_bundle(runtime, core_rlib):
    a, b = archive_manifest(Path(runtime).read_bytes()), archive_manifest(Path(core_rlib).read_bytes())
    require(len(a) == 1438, 'Unexpected native archive size')
    pair = lambda rows: [(x['name'], x['sha256']) for x in rows]
    intended, linked = pair(a), pair(b)
    ia = [h for name, h in intended if name == 'activations.cc.o']
    la = [h for name, h in linked if name == 'activations.cc.o']
    require(len(ia) == 2 and la == ia, 'Linked CORE activation occurrences differ')
    dispatcher = [h for name, h in intended if name == 'gemm-config.c.o']
    require(len(dispatcher) == 1 and dispatcher == [h for name, h in linked if name == 'gemm-config.c.o'], 'Linked CORE dispatcher differs')
    cursor = 0
    for member in linked:
        if cursor < len(intended) and member == intended[cursor]:
            cursor += 1
    require(cursor == len(intended), 'Linked CORE lacks all ordered intended native payloads')
    # Disallow extra payloads with any native member name, even if a subsequence matched.
    native_names = {name for name, _ in intended}
    require([m for m in linked if m[0] in native_names] == intended, 'Extra or reordered native members in CORE')
    return {'activation_member_sha256': ia[0], 'activation_occurrence': 1, 'duplicate_activation_sha256': ia[1], 'all_ordered_native_members_verified': len(a), 'ordered_native_members_sha256': sha(__import__('json').dumps(intended, separators=(',', ':')).encode()), 'dispatcher_sha256': dispatcher[0], 'linked_core_rlib_sha256': file_sha(core_rlib)}
