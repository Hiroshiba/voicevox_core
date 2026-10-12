"""Deterministic archive encoding; CLI is execution-blocked before imports."""
if __name__ == '__main__':
    raise SystemExit('SOURCE_ONLY_EXECUTION_BLOCK: launch.archive_recipe')

import hashlib
from launch.source_only import require_execution_admission


def parse(raw):
    if raw[:8] != b'!<arch>\n':
        raise ValueError('archive_magic')
    at, names, members = 8, b'', []
    while at < len(raw):
        header = raw[at:at+60]
        if len(header) != 60 or header[58:] != b'`\n':
            raise ValueError('archive_header')
        size = int(header[48:58]); data = raw[at+60:at+60+size]
        if len(data) != size:
            raise ValueError('archive_extent')
        at += 60 + size + size % 2
        name = header[:16].decode().strip()
        if name == '//': names = data; continue
        if name in ('/', '/SYM64/'): continue
        if name.startswith('#1/'): raise ValueError('unsupported_archive_name')
        if name.startswith('/'):
            offset = int(name[1:])
            if not 0 <= offset < len(names): raise ValueError('archive_name_offset')
            name = names[offset:].split(b'/\n', 1)[0].decode()
        else: name = name.removesuffix('/')
        members.append((name, data))
    if at != len(raw): raise ValueError('archive_trailing_bytes')
    return members


def inventory(members):
    return [{'index': i+1, 'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for i, (name, data) in enumerate(members)]


def encode(members):
    names, offsets = b'', {}
    for name, _ in members:
        if len(name.encode()) > 15 and name not in offsets:
            offsets[name] = len(names); names += name.encode() + b'/\n'
    def entry(name, data):
        header = name.encode().ljust(16)+b'0'.ljust(12)+b'0'.ljust(6)+b'0'.ljust(6)+b'644'.ljust(8)+str(len(data)).encode().ljust(10)+b'`\n'
        if len(header) != 60: raise ValueError('archive_header_width')
        return header + data + (b'\n' if len(data) % 2 else b'')
    chunks = [b'!<arch>\n']
    if names: chunks.append(entry('//', names))
    for name, data in members: chunks.append(entry('/'+str(offsets[name]) if name in offsets else name+'/', data))
    return b''.join(chunks)


def prepare_variants(baseline, expected_members, replacements):
    original = parse(baseline)
    if len(original) != 1438 or inventory(original) != expected_members: raise ValueError('baseline_exact_members')
    if original[793][0] != 'convolution-nhwc.c.o': raise ValueError('convolution_member')
    if set(replacements) != {'A','B','Q','P'}: raise ValueError('variant_roster')
    result = {}
    for arm in ('A','B','Q','P'):
        values = replacements[arm]
        if set(values) != ({'convolution'} if arm == 'A' else {'convolution','callback'}): raise ValueError('replacement_roster')
        members = original.copy(); members[793] = (original[793][0], values['convolution'])
        if arm != 'A': members.append(('vvx_ksb_20261011.c.o', values['callback']))
        result[arm] = encode(members)
        decoded = parse(result[arm])
        if any(decoded[i] != original[i] for i in range(1438) if i != 793): raise ValueError('preserved_members')
    return result


def run(baseline_path, destination, expected_members):
    require_execution_admission('archive_recipe.run')
    from pathlib import Path
    baseline_path, destination = Path(baseline_path), Path(destination)
    replacements = {arm: {'convolution': (destination/f'{arm}_convolution.o').read_bytes(), **({} if arm == 'A' else {'callback': (destination/f'{arm}_callback.o').read_bytes()})} for arm in ('A','B','Q','P')}
    for arm, data in prepare_variants(baseline_path.read_bytes(), expected_members, replacements).items():
        with (destination/f'{arm}.a').open('xb') as stream: stream.write(data)
    with (destination/'archive-scope.json').open('x') as stream:
        import json
        json.dump({'schema':'panel-archive-scope-v1','baseline_members':expected_members,'replacement_index':794,'arms':['A','B','Q','P']},stream,sort_keys=True)
    for index in (794,1240):
        with (destination/f'archived_{index}.o').open('xb') as stream: stream.write(parse(baseline_path.read_bytes())[index-1][1])

