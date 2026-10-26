"""Extract members from X3 TC/AP .cat/.dat catalogues.

Index (.cat): every byte XOR (0xDB + i) & 0xFF; first line is the .dat name, then "path size" lines;
members lie back to back in the .dat, every byte XOR 0x33.

    x3kc cat <cat> [pattern] [-o outdir]     list (or extract with -o) members whose path contains pattern
"""
import argparse
import os
import sys


def norm(p):
    return p.replace(chr(92), '/').lower()


def read_index(cat):
    b = open(cat, 'rb').read()
    t = bytes((c ^ ((0xDB + i) & 0xFF)) for i, c in enumerate(b)).decode('latin1')
    lines = [l for l in t.splitlines() if l.strip()]
    out, off = [], 0
    for l in lines[1:]:
        path, size = l.rsplit(' ', 1)
        out.append((path, off, int(size)))
        off += int(size)
    return out


def read_member(cat, path):
    for p, off, size in read_index(cat):
        if norm(p) == norm(path):
            with open(os.path.splitext(cat)[0] + '.dat', 'rb') as f:
                f.seek(off)
                return bytes(c ^ 0x33 for c in f.read(size))
    raise KeyError(path)


def write_catalog(cat, members):
    """Write cat/dat with members [(path inside, bytes)]; paths with forward slashes, as in the game's catalogs
    (their first line, the .dat name, is not used: the stock ones say foo.dat)."""
    dat = os.path.splitext(cat)[0] + '.dat'
    os.makedirs(os.path.dirname(os.path.abspath(cat)), exist_ok=True)
    lines = [os.path.basename(dat)] + ['%s %d' % (p, len(b)) for p, b in members]
    idx = ('\n'.join(lines) + '\n').encode('latin1')
    open(cat, 'wb').write(bytes((c ^ ((0xDB + i) & 0xFF)) for i, c in enumerate(idx)))
    with open(dat, 'wb') as f:
        for _, b in members:
            f.write(bytes(c ^ 0x33 for c in b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cat')
    ap.add_argument('pattern', nargs='?', default='')
    ap.add_argument('-o', '--out')
    ap.add_argument('--pack', nargs='+', metavar='PATH=FILE',
                    help='write a new catalog <cat> holding FILE as PATH, e.g. L/x3story.obj=my.obj')
    a = ap.parse_args()
    if a.pack:
        members = []
        for spec in a.pack:
            path, f = spec.split('=', 1)
            members.append((path, open(f, 'rb').read()))
        write_catalog(a.cat, members)
        for p, off, size in read_index(a.cat):
            print('packed', p, size)
        return 0
    for p, off, size in read_index(a.cat):
        if a.pattern.lower() in p.lower():
            print(p, size)
            if a.out:
                dst = os.path.join(a.out, norm(p))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                open(dst, 'wb').write(read_member(a.cat, p))


if __name__ == '__main__':
    sys.exit(main())
