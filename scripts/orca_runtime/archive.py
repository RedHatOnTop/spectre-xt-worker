"""Read and reproducibly repack Electron ASAR files without editing installations."""
import hashlib
import json
from pathlib import Path
import struct

BLOCK_BYTES = 4 * 1024 * 1024


def index(path):
    with Path(path).open('rb') as source:
        size, length = struct.unpack('<II', source.read(8))
        if size != 4 or not 8 <= length <= 32 * 1024 * 1024:
            raise ValueError('archive header has invalid pickle size')
        header = source.read(length)
        count = struct.unpack('<I', header[4:8])[0]
        return json.loads(header[8:8 + count].rstrip(b'\x00')), 8 + length


def entries(header, prefix=''):
    result = {}
    for name, value in header.get('files', {}).items():
        if name in ('.', '..') or '/' in name or '\\' in name:
            raise ValueError('archive entry has an unsafe path')
        path = f'{prefix}/{name}' if prefix else name
        result = {**result, **entries(value, path)} if 'files' in value else {**result, path: value}
    return result


def read(path, name):
    header, offset = index(path)
    entry = entries(header)[name]
    return read_entry(path, offset, entry, name)


def read_entry(path, offset, entry, name):
    if entry.get('link'):
        raise ValueError('archive symbolic entries are not supported')
    if entry.get('unpacked'):
        return (Path(str(path) + '.unpacked') / name).read_bytes()
    with Path(path).open('rb') as source:
        source.seek(offset + int(entry['offset']))
        data = source.read(entry['size'])
    if len(data) != entry['size']:
        raise ValueError('archive entry is truncated')
    return data


def integrity(data):
    return {'algorithm': 'SHA256', 'hash': hashlib.sha256(data).hexdigest(),
            'blockSize': BLOCK_BYTES, 'blocks': [hashlib.sha256(data[i:i + BLOCK_BYTES]).hexdigest()
                                               for i in range(0, len(data), BLOCK_BYTES)]}


def layout(node, content, prefix='', offset=0):
    rows = {}
    for name, entry in node.get('files', {}).items():
        path = f'{prefix}/{name}' if prefix else name
        if 'files' in entry:
            value, offset = layout(entry, content, path, offset)
        elif entry.get('unpacked'):
            data = content.get(path)
            value = {**entry, **({'size': len(data), 'integrity': integrity(data)} if data is not None else {})}
        else:
            data = content[path]
            value = {**entry, 'size': len(data), 'offset': str(offset), 'integrity': integrity(data)}
            offset += len(data)
        rows = {**rows, name: value}
    return {**node, 'files': rows}, offset


def write(path, header, content):
    updated, _ = layout(header, content)
    body = json.dumps(updated, separators=(',', ':')).encode()
    padded = (len(body) + 4) // 4 * 4
    pickle_body = struct.pack('<II', padded + 4, len(body)) + body + b'\x00' * (padded - len(body))
    with Path(path).open('xb') as output:
        output.write(struct.pack('<II', 4, len(pickle_body)))
        output.write(pickle_body)
        for name, entry in entries(updated).items():
            if not entry.get('unpacked'):
                output.write(content[name])
    return updated


def patch(source, target, changes):
    if Path(target).exists() or Path(target).resolve() == Path(source).resolve():
        raise ValueError('archive output must be new and separate from the installation')
    header, offset = index(source)
    rows = entries(header)
    if not set(changes) <= set(rows):
        raise ValueError('archive patch cannot introduce undeclared entries')
    content = {name: changes[name] if name in changes else read_entry(source, offset, entry, name) for name, entry in rows.items()
               if not entry.get('unpacked')}
    write(target, header, {**content, **changes})
    for name, data in changes.items():
        if rows[name].get('unpacked'):
            path = Path(str(target) + '.unpacked') / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return {'changed': sorted(changes), 'archive_sha256': hashlib.sha256(Path(target).read_bytes()).hexdigest()}


def verify(path):
    header, offset = index(path)
    for name, entry in entries(header).items():
        data = read_entry(path, offset, entry, name)
        if entry.get('integrity') != integrity(data):
            raise ValueError(f'archive integrity mismatch: {name}')
    return True
