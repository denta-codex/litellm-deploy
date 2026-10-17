"""Read the chat template embedded in a GGUF header without loading weights.

--check SHA256 fails when the embedded template differs from the one the
committed override was derived from. --extract PATH writes the embedded
template so the override can be regenerated after a model update.
"""
import argparse
import hashlib
from pathlib import Path
import struct
import sys

KEY = 'tokenizer.chat_template'
# GGUF metadata value types: fixed-size scalars by type id, 8 = string, 9 = array.
SCALARS = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def read(f, size):
    data = f.read(size)
    if len(data) != size:
        raise ValueError('Truncated GGUF header')
    return data


def string(f):
    return read(f, struct.unpack('<Q', read(f, 8))[0])


def skip(f, kind):
    if kind in SCALARS:
        read(f, SCALARS[kind])
    elif kind == 8:
        string(f)
    elif kind == 9:
        item, count = struct.unpack('<IQ', read(f, 12))
        if item in SCALARS:
            f.seek(SCALARS[item] * count, 1)
        else:
            for _ in range(count):
                skip(f, item)
    else:
        raise ValueError(f'Unknown GGUF value type {kind}')


def embedded_template(path):
    with open(path, 'rb') as f:
        if read(f, 4) != b'GGUF':
            raise ValueError(f'{path} is not a GGUF file')
        _version, _tensors, count = struct.unpack('<IQQ', read(f, 20))
        for _ in range(count):
            key = string(f).decode()
            kind = struct.unpack('<I', read(f, 4))[0]
            if key == KEY and kind == 8:
                return string(f)
            skip(f, kind)
    raise ValueError(f'{path} has no {KEY}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('model', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', metavar='SHA256')
    action.add_argument('--extract', metavar='PATH', type=Path)
    args = parser.parse_args()
    template = embedded_template(args.model)
    digest = hashlib.sha256(template).hexdigest()
    if args.extract:
        args.extract.write_bytes(template)
        print(digest)
    elif digest != args.check:
        sys.exit(f'GGUF chat template changed ({digest}); regenerate '
                 'deploy/files/qwen3.8-27b-chat-template.jinja and update both checksums.')
    else:
        print(f'Embedded chat template matches {digest}')


if __name__ == '__main__':
    main()
