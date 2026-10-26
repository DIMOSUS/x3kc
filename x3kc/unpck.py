"""Unpack X3 .pck files (gzip, optionally XOR-masked with the first byte ^ 0xC8) into plain files.

    x3kc unpck <src dir> <dst dir> [ext]      .pck -> .xml (ext), other files copied as they are
"""
import gzip
import os
import shutil
import sys


def unpack(b):
    try:
        return gzip.decompress(b)
    except OSError:
        k = b[0] ^ 0xC8
        return gzip.decompress(bytes(x ^ k for x in b[1:]))


def main():
    src, dst = sys.argv[1], sys.argv[2]
    ext = sys.argv[3] if len(sys.argv) > 3 else '.xml'
    os.makedirs(dst, exist_ok=True)
    n = bad = 0
    for f in sorted(os.listdir(src)):
        p = os.path.join(src, f)
        if not os.path.isfile(p):
            continue
        if f.lower().endswith('.pck'):
            try:
                data = unpack(open(p, 'rb').read())         # before opening: a failure keeps the old file
                open(os.path.join(dst, f[:-4] + ext), 'wb').write(data)
                n += 1
            except Exception as x:
                bad += 1
                print('failed', f, x)
        else:
            shutil.copyfile(p, os.path.join(dst, f))
            n += 1
    print(n, 'files,', bad, 'failed')


if __name__ == '__main__':
    main()
